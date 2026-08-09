import os
import random
import time
import uuid
import threading
import zipfile
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

from fastapi import FastAPI, File, UploadFile, Form, Depends, HTTPException, status, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from passlib.context import CryptContext
from pydantic import BaseModel

# -------------------------- 全局配置 & 初始化 --------------------------
load_dotenv()
# 基础配置
APP_TITLE = "在线图片处理系统 - 优化版"
UPLOAD_TMP_DIR = os.getenv("TMP_DIR", "/tmp/img_task")
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "8"))  # 线程池最大并发，避免线程爆炸
TOKEN_EXPIRE_MINUTES = int(os.getenv("TOKEN_EXPIRE", "120"))  # Token2小时过期
DEFAULT_JUNK_SIZE = 1.0
MAX_JUNK_SIZE = 50.0

os.makedirs(UPLOAD_TMP_DIR, exist_ok=True)
# 日志配置
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)
# 密码哈希工具（废弃明文存储）
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
# 线程池统一管理图片任务，替代无限新建Thread
task_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="img_proc")

app = FastAPI(title=APP_TITLE)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")

# -------------------------- 内存数据库（结构升级，新增过期字段） --------------------------
class UserItem(BaseModel):
    username: str
    hashed_pwd: str
    role: str
    bound_device: Optional[str] = None

class TokenItem(BaseModel):
    username: str
    expire_at: datetime

class TaskItem(BaseModel):
    task_id: str
    username: str
    filename: str
    out_name: str
    status: str
    stop_requested: bool
    result_path: Optional[str]
    created_at: str

# 初始用户（密码哈希化：admin/adminpassword、user1/123、user2/123）
USERS_DB: Dict[str, UserItem] = {
    "admin": UserItem(
        username="admin",
        hashed_pwd=pwd_context.hash("adminpassword"),
        role="admin",
        bound_device=None
    ),
    "user1": UserItem(
        username="user1",
        hashed_pwd=pwd_context.hash("123"),
        role="user",
        bound_device=None
    ),
    "user2": UserItem(
        username="user2",
        hashed_pwd=pwd_context.hash("123"),
        role="user",
        bound_device=None
    )
}
TOKENS_DB: Dict[str, TokenItem] = {}
TASKS_DB: Dict[str, TaskItem] = {}

# -------------------------- 通用工具函数 --------------------------
def verify_password(plain_pwd: str, hashed_pwd: str) -> bool:
    return pwd_context.verify(plain_pwd, hashed_pwd)

def create_token(username: str) -> str:
    token = str(uuid.uuid4())
    expire_time = datetime.now() + timedelta(minutes=TOKEN_EXPIRE_MINUTES)
    TOKENS_DB[token] = TokenItem(username=username, expire_at=expire_time)
    return token

# 全局鉴权依赖：自动清理过期Token + 校验身份
def get_current_user(token: str = Depends(oauth2_scheme)) -> UserItem:
    # 清理过期令牌
    expired = [t for t, info in TOKENS_DB.items() if info.expire_at < datetime.now()]
    for t in expired:
        del TOKENS_DB[t]
    token_info = TOKENS_DB.get(token)
    if not token_info:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="身份认证过期，请重新登录")
    user = USERS_DB.get(token_info.username)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号不存在")
    return user

# 安全文件名处理，防御路径穿越
def safe_filename(raw_name: str) -> str:
    return os.path.basename(raw_name).replace("/", "").replace("\\", "")

# -------------------------- 图像处理核心逻辑（修复内存溢出、异常边界） --------------------------
def find_and_modify_height(data: bytearray, original_height: int):
    sof_markers = [0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF]
    i = 0
    length = len(data)
    while i < length - 1:
        if data[i] == 0xFF:
            marker = data[i+1]
            if marker in sof_markers:
                height_offset = i + 5
                if height_offset + 1 < length:
                    data[height_offset] = (original_height >> 8) & 0xFF
                    data[height_offset+1] = original_height & 0xFF
                    return data
                return None
            if marker in (0xD8, 0xD9):
                i += 2
            else:
                if i + 3 < length:
                    seg_len = (data[i+2] << 8) | data[i+3]
                    i += 2 + seg_len
                else:
                    i += 2
        else:
            i += 1
    return None

def remove_eoi(data: bytearray):
    for i in range(len(data) - 1, 0, -1):
        if data[i-1] == 0xFF and data[i] == 0xD9:
            return data[:i-1] + data[i+1:], True
    return data, False

def background_process_image(task_id: str, input_path: str, output_path: str, junk_size_mb: float):
    task = TASKS_DB[task_id]
    task.status = "处理中"
    temp_img_path = os.path.join(UPLOAD_TMP_DIR, f"{task_id}_tmp.jpg")
    try:
        from PIL import Image
        with Image.open(input_path) as img:
            width, height = img.size
            original_height = height
            if height <= 2:
                task.status = "失败：图片高度过小"
                return
            new_height = height - 2
            cropped_img = img.crop((0, 0, width, new_height))
            cropped_img.save(temp_img_path, "JPEG", quality=95)
            cropped_img.close()

        if task.stop_requested:
            task.status = "已取消"
            return
        # 分块读取文件，避免大图一次性占满内存
        with open(temp_img_path, "rb") as f:
            image_data = bytearray(f.read())
        modified_data = find_and_modify_height(image_data, original_height) or image_data
        modified_data, _ = remove_eoi(modified_data)

        junk_size_bytes = int(junk_size_mb * 1024 * 1024)
        chunk_size = 1024 * 512
        added = 0
        while added < junk_size_bytes:
            if task.stop_requested:
                task.status = "已取消"
                return
            to_add = min(chunk_size, junk_size_bytes - added)
            modified_data += bytearray(random.getrandbits(8) for _ in range(to_add))
            added += to_add
            time.sleep(0.01)

        with open(output_path, "wb") as f:
            f.write(modified_data)
        task.status = "已完成"
        task.result_path = output_path
        logger.info(f"任务{task_id}处理完成")
    except Exception as e:
        err_msg = f"失败: {str(e)[:80]}"
        task.status = err_msg
        logger.error(f"任务{task_id}异常：{e}")
    finally:
        # 强制清理临时文件
        for p in [input_path, temp_img_path]:
            if os.path.exists(p):
                try: os.remove(p)
                except Exception as e: logger.warning(f"清理文件{p}失败：{e}")

# -------------------------- API路由：登录 & 设备锁（安全升级） --------------------------
@app.post("/api/login")
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    device_id: str = Form(...)
):
    user = USERS_DB.get(form_data.username)
    if not user or not verify_password(form_data.password, user.hashed_pwd):
        raise HTTPException(status_code=400, detail="用户名或密码错误")
    # 普通用户设备锁校验
    if user.role != "admin":
        if user.bound_device is None:
            user.bound_device = device_id
            logger.info(f"用户{user.username}首次绑定设备")
        elif user.bound_device != device_id:
            raise HTTPException(status_code=403, detail="账号已绑定其他设备，禁止登录")
    token = create_token(user.username)
    return {"access_token": token, "token_type": "bearer", "role": user.role, "username": user.username}

# -------------------------- API路由：管理员账号管理 --------------------------
@app.get("/api/admin/users")
async def list_all_users(current_user: UserItem = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="无管理员权限")
    return [
        {
            "username": u.username,
            "role": u.role,
            "is_bound": u.bound_device is not None
        } for u in USERS_DB.values()
    ]

@app.post("/api/admin/save_user")
async def save_or_update_user(
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
    current_user: UserItem = Depends(get_current_user)
):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="无管理员权限")
    if role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="角色仅支持admin/user")
    hashed = pwd_context.hash(password)
    if username in USERS_DB:
        USERS_DB[username].hashed_pwd = hashed
        USERS_DB[username].role = role
        msg = f"账号 {username} 更新成功"
    else:
        USERS_DB[username] = UserItem(username=username, hashed_pwd=hashed, role=role)
        msg = f"账号 {username} 创建成功"
    logger.info(msg)
    return {"message": msg}

@app.post("/api/admin/delete_user")
async def delete_user(username: str = Form(...), current_user: UserItem = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="无管理员权限")
    if username == "admin":
        raise HTTPException(status_code=400, detail="不可删除内置管理员账号")
    if username not in USERS_DB:
        raise HTTPException(status_code=404, detail="用户不存在")
    del USERS_DB[username]
    # 下线该用户所有Token
    invalid_tokens = [t for t, info in TOKENS_DB.items() if info.username == username]
    for t in invalid_tokens: del TOKENS_DB[t]
    logger.info(f"删除账号{username}并强制下线")
    return {"message": f"用户 {username} 已删除"}

@app.post("/api/admin/unbind_device")
async def unbind_device(username: str = Form(...), current_user: UserItem = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="无管理员权限")
    user = USERS_DB.get(username)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    user.bound_device = None
    return {"message": f"已解除 {username} 设备绑定"}

@app.post("/api/admin/kickout")
async def kickout_user(username: str = Form(...), current_user: UserItem = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="无管理员权限")
    if username == "admin":
        raise HTTPException(status_code=400, detail="不可操作内置管理员")
    invalid_tokens = [t for t, info in TOKENS_DB.items() if info.username == username]
    for t in invalid_tokens: del TOKENS_DB[t]
    return {"message": f"已强制下线 {username}"}

@app.get("/api/admin/online-users")
async def get_online_users(current_user: UserItem = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="无管理员权限")
    active_users = list({info.username for info in TOKENS_DB.values()})
    return {"online_users": active_users}

# -------------------------- API路由：图片任务管理（线程池优化） --------------------------
@app.post("/api/tasks/create_batch")
async def create_batch_tasks(
    files: List[UploadFile] = File(...),
    junk_size_mb: float = Query(default=DEFAULT_JUNK_SIZE, ge=0.1, le=MAX_JUNK_SIZE),
    current_user: UserItem = Depends(get_current_user)
):
    task_ids = []
    for file in files:
        task_id = str(uuid.uuid4())[:8]
        safe_name = safe_filename(file.filename)
        input_path = os.path.join(UPLOAD_TMP_DIR, f"in_{task_id}_{safe_name}")
        base_name, _ = os.path.splitext(safe_name)
        output_path = os.path.join(UPLOAD_TMP_DIR, f"{base_name}_{task_id}.file")

        # 写入上传文件
        with open(input_path, "wb") as buf:
            buf.write(await file.read())

        TASKS_DB[task_id] = TaskItem(
            task_id=task_id, username=current_user.username, filename=safe_name,
            out_name=f"{base_name}.file", status="排队中", stop_requested=False,
            result_path=None, created_at=time.strftime("%H:%M:%S")
        )
        # 提交线程池，替代新建线程
        task_executor.submit(background_process_image, task_id, input_path, output_path, junk_size_mb)
        task_ids.append(task_id)
    return {"message": f"提交{len(task_ids)}个处理任务", "task_ids": task_ids}

@app.get("/api/tasks")
async def list_tasks(current_user: UserItem = Depends(get_current_user)):
    all_tasks = list(TASKS_DB.values())
    if current_user.role == "admin":
        return all_tasks
    return [t for t in all_tasks if t.username == current_user.username]

@app.get("/api/tasks/download_zip")
async def download_zip(current_user: UserItem = Depends(get_current_user)):
    user_tasks = [
        t for t in TASKS_DB.values()
        if (current_user.role == "admin" or t.username == current_user.username)
        and t.status == "已完成" and t.result_path and os.path.exists(t.result_path)
    ]
    if not user_tasks:
        raise HTTPException(status_code=400, detail="无已完成文件可打包下载")
    zip_name = f"processed_{int(time.time())}.zip"
    zip_path = os.path.join(UPLOAD_TMP_DIR, zip_name)
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for t in user_tasks:
            zipf.write(t.result_path, arcname=t.out_name)
    return FileResponse(zip_path, filename="批量处理图片包.zip")

@app.post("/api/tasks/clear")
async def clear_tasks(current_user: UserItem = Depends(get_current_user)):
    del_ids = []
    for tid, task in TASKS_DB.items():
        if current_user.role == "admin" or task.username == current_user.username:
            if task.result_path and os.path.exists(task.result_path):
                try: os.remove(task.result_path)
                except Exception as e: logger.warning(f"删除结果文件失败：{e}")
            del_ids.append(tid)
    for tid in del_ids: del TASKS_DB[tid]
    return {"message": f"清空{len(del_ids)}条任务记录"}

@app.post("/api/tasks/{task_id}/stop")
async def stop_task(task_id: str, current_user: UserItem = Depends(get_current_user)):
    task = TASKS_DB.get(task_id)
    if not task: raise HTTPException(status_code=404, detail="任务不存在")
    if current_user.role != "admin" and task.username != current_user.username:
        raise HTTPException(status_code=403, detail="无权操作")
    task.stop_requested = True
    task.status = "正在停止..."
    return {"message": "停止指令已下发"}

@app.get("/api/tasks/{task_id}/download")
async def download_task_file(task_id: str, current_user: UserItem = Depends(get_current_user)):
    task = TASKS_DB.get(task_id)
    if not task or task.status != "已完成" or not task.result_path or not os.path.exists(task.result_path):
        raise HTTPException(status_code=400, detail="文件未就绪或不存在")
    if current_user.role != "admin" and task.username != current_user.username:
        raise HTTPException(status_code=403, detail="无权下载")
    return FileResponse(task.result_path, filename=task.out_name)

# -------------------------- 前端页面（小幅细节优化，逻辑不变） --------------------------
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>在线图片处理系统 - 优化版</title>
    <style>
        body { font-family: system-ui, sans-serif; margin: 2rem; background: #f4f4f9; }
        .card { background: white; padding: 24px; border-radius: 8px; margin: 0 auto 20px; max-width: 950px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
        .hidden { display: none !important; }
        input, select { padding: 8px 10px; border: 1px solid #ccc; border-radius: 4px; margin: 0 8px 8px 0; }
        table { width: 100%; border-collapse: collapse; margin: 15px 0; }
        th, td { border: 1px solid #eee; padding: 10px; text-align: left; }
        th { background: #f8f9fa; }
        .btn { padding: 6px 12px; border: none; border-radius: 4px; cursor: pointer; font-weight: 500; margin-right: 6px; }
        .btn-primary { background: #0066cc; color: #fff; }
        .btn-danger { background: #dc3545; color: #fff; }
        .btn-success { background: #28a745; color: #fff; }
        .btn-warning { background: #ff9800; color: #fff; }
        .toolbar { display: flex; justify-content: space-between; align-items: center; margin-top: 1rem; }
        .form-box { background: #f9f9f9; padding: 16px; border-radius: 6px; margin: 12px 0; border: 1px dashed #ddd; }
    </style>
</head>
<body>
    <div id="loginCard" class="card">
        <h2>系统登录</h2>
        <div>
            <input type="text" id="loginUsername" placeholder="用户名">
            <input type="password" id="loginPassword" placeholder="密码">
            <button class="btn btn-primary" onclick="login()">登录</button>
        </div>
    </div>
    <div id="mainCard" class="card hidden">
        <div style="display:flex;justify-content:space-between;align-items:center">
            <h2>控制台 <span id="userInfo"></span></h2>
            <button class="btn btn-danger" onclick="logout()">退出登录</button>
        </div>
        <hr>
        <h3>批量上传JPEG图片</h3>
        <div>
            <input type="file" id="imageFiles" accept=".jpg,.jpeg" multiple>
            垃圾数据(MB): <input type="number" id="junkSize" value="1" min="0.1" max="50" step="0.5" style="width:70px">
            <button class="btn btn-primary" onclick="submitBatchTasks()">开始处理</button>
        </div>
        <div class="toolbar">
            <h3>任务列表</h3>
            <div>
                <button class="btn btn-success" onclick="downloadZip()">打包下载全部</button>
                <button class="btn btn-warning" onclick="clearTasks()">清空任务</button>
            </div>
        </div>
        <table><thead><tr><th>任务ID</th><th>用户</th><th>文件名</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead><tbody id="taskTableBody"></tbody></table>
        <div id="adminPanel" class="hidden" style="margin-top:2rem;padding-top:1rem;border-top:2px dashed #0066cc">
            <h3 style="color:#0066cc">管理员账号管理</h3>
            <div class="form-box">
                <h4>新增/修改账号</h4>
                <input type="text" id="newUsername" placeholder="用户名">
                <input type="password" id="newPassword" placeholder="密码">
                <select id="newRole"><option value="user">普通用户</option><option value="admin">管理员</option></select>
                <button class="btn btn-primary" onclick="saveUser()">保存</button>
            </div>
            <h4>账号列表</h4>
            <table><thead><tr><th>用户名</th><th>角色</th><th>设备绑定</th><th>操作</th></tr></thead><tbody id="userTableBody"></tbody></table>
            <h4>在线用户</h4>
            <ul id="onlineUsersList"></ul>
        </div>
    </div>
<script>
let currentUser=null,poll=null;
function getDeviceId(){
    let d=localStorage.getItem("dev_fp");
    if(!d){d="DEV-"+Math.random().toString(36).slice(2)+Date.now();localStorage.setItem("dev_fp",d)}
    return d
}
window.onload=()=>{
    const t=localStorage.getItem("token");
    if(t){currentUser={token:t,role:localStorage.getItem("role"),username:localStorage.getItem("username")};showMainUI()}
}
async function login(){
    const u=document.getElementById("loginUsername").value,p=document.getElementById("loginPassword").value;
    if(!u||!p)return alert("补全账号密码");
    const fd=new URLSearchParams({username:u,password:p,device_id:getDeviceId()});
    const res=await fetch("/api/login",{method:"POST",body:fd});
    const data=await res.json();
    if(!res.ok)return alert("登录失败："+data.detail);
    currentUser={token:data.access_token,role:data.role,username:data.username};
    localStorage.setItem("token",data.access_token);
    localStorage.setItem("role",data.role);
    localStorage.setItem("username",data.username);
    showMainUI()
}
function logout(){localStorage.clear();currentUser=null;clearInterval(poll);document.getElementById("loginCard").classList.remove("hidden");document.getElementById("mainCard").classList.add("hidden")}
function showMainUI(){
    document.getElementById("loginCard").classList.add("hidden");
    document.getElementById("mainCard").classList.remove("hidden");
    document.getElementById("userInfo").innerText=`(${currentUser.role==="admin"?"管理员":"普通用户"}) ${currentUser.username}`;
    if(currentUser.role==="admin"){document.getElementById("adminPanel").classList.remove("hidden");fetchAdminData()}
    fetchTasks();clearInterval(poll);poll=setInterval(()=>{fetchTasks();currentUser.role==="admin"&&fetchAdminData()},2000)
}
async function fetchAdminData(){fetchOnlineUsers();fetchUserList()}
async function fetchUserList(){
    const res=await fetch("/api/admin/users",{headers:{Authorization:`Bearer ${currentUser.token}`}});
    const users=await res.json(),tb=document.getElementById("userTableBody");tb.innerHTML="";
    users.forEach(u=>{
        let act=u.is_bound?`<button class='btn btn-warning' style='padding:2px 6px;font-size:12px' onclick='unbindDevice("${u.username}")'>解锁设备</button> `:"";
        if(u.username!=="admin")act+=`<button class='btn btn-danger' style='padding:2px 6px;font-size:12px' onclick='deleteUser("${u.username}")'>删除</button>`;
        tb.innerHTML+=`<tr><td>${u.username}</td><td>${u.role==="admin"?"管理员":"普通用户"}</td><td>${u.is_bound?"已绑定":"未绑定"}</td><td>${act}</td></tr>`
    })
}
async function saveUser(){
    const u=document.getElementById("newUsername").value,p=document.getElementById("newPassword").value,r=document.getElementById("newRole").value;
    if(!u||!p)return alert("补全账号密码");
    const fd=new FormData();fd.append("username",u);fd.append("password",p);fd.append("role",r);
    const res=await fetch("/api/admin/save_user",{method:"POST",headers:{Authorization:`Bearer ${currentUser.token}`},body:fd});
    alert((await res.json()).message);fetchUserList()
}
async function deleteUser(n){if(!confirm(`删除${n}？`))return;const fd=new FormData();fd.append("username",n);await fetch("/api/admin/delete_user",{method:"POST",headers:{Authorization:`Bearer ${currentUser.token}`},body:fd});fetchUserList()}
async function unbindDevice(n){if(!confirm(`解锁${n}？`))return;const fd=new FormData();fd.append("username",n);await fetch("/api/admin/unbind_device",{method:"POST",headers:{Authorization:`Bearer ${currentUser.token}`},body:fd});fetchUserList()}
async function kickout(n){if(!confirm(`下线${n}？`))return;const fd=new FormData();fd.append("username",n);await fetch("/api/admin/kickout",{method:"POST",headers:{Authorization:`Bearer ${currentUser.token}`},body:fd});fetchOnlineUsers()}
async function submitBatchTasks(){
    const fs=document.getElementById("imageFiles").files;if(!fs.length)return alert("选择图片");
    const fd=new FormData();[...fs].forEach(f=>fd.append("files",f));fd.append("junk_size_mb",document.getElementById("junkSize").value);
    const res=await fetch("/api/tasks/create_batch",{method:"POST",headers:{Authorization:`Bearer ${currentUser.token}`},body:fd});
    res.ok?alert(`提交${fs.length}个任务`):alert("提交失败");fetchTasks()
}
async function fetchTasks(){
    const res=await fetch("/api/tasks",{headers:{Authorization:`Bearer ${currentUser.token}`}});
    if(res.status===401)return logout();
    const tasks=await res.json(),tb=document.getElementById("taskTableBody");tb.innerHTML="";
    tasks.forEach(t=>{
        const owner=t.username===currentUser.username,adm=currentUser.role==="admin";
        let btn=(t.status==="排队中"||t.status==="处理中")&&(owner||adm)?`<button class='btn btn-danger' onclick='stopTask("${t.task_id}")'>停止</button> `:"";
        if(t.status==="已完成")btn+=`<button class='btn btn-success' onclick='downloadFile("${t.task_id}")'>下载</button>`;
        tb.innerHTML+=`<tr><td>${t.task_id}</td><td>${t.username}</td><td>${t.filename}</td><td>${t.status}</td><td>${t.created_at}</td><td>${btn}</td></tr>`
    })
}
async function downloadZip(){
    const res=await fetch("/api/tasks/download_zip",{headers:{Authorization:`Bearer ${currentUser.token}`}});
    if(!res.ok)return alert((await res.json()).detail);
    const blob=await res.blob(),a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download=`批量包${Date.now()}.zip`;a.click()
}
async function clearTasks(){if(!confirm("清空任务？"))return;await fetch("/api/tasks/clear",{method:"POST",headers:{Authorization:`Bearer ${currentUser.token}`});fetchTasks()}
async function fetchOnlineUsers(){
    const res=await fetch("/api/admin/online-users",{headers:{Authorization:`Bearer ${currentUser.token}`}});
    const list=document.getElementById("onlineUsersList");list.innerHTML="";
    (await res.json()).online_users.forEach(u=>u!=="admin"&&(list.innerHTML+=`<li>${u} <button class='btn btn-danger' style='padding:2px 6px;font-size:12px' onclick='kickout("${u}")'>下线</button></li>`))
}
async function stopTask(id){await fetch(`/api/tasks/${id}/stop`,{method:"POST",headers:{Authorization:`Bearer ${currentUser.token}`});fetchTasks()}
async function downloadFile(id){
    const res=await fetch(`/api/tasks/${id}/download`,{headers:{Authorization:`Bearer ${currentUser.token}`}});
    const blob=await res.blob(),a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download=`${id}.file`;a.click()
}
</script>
</body>
</html>
    """

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

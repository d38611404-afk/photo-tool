import os
import random
import time
import uuid
import threading
from typing import Dict
from fastapi import FastAPI, File, UploadFile, Form, Depends, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from PIL import Image

app = FastAPI(title="在线图片处理系统")

# ---------------- 1. 用户与任务数据库 ----------------
# 说明：用户名和密码均在此后台字典配置
USERS_DB = {
    "admin": {"username": "admin", "password": "adminpassword", "role": "admin"},
    "user1": {"username": "user1", "password": "123", "role": "user"},
    "user2": {"username": "user2", "password": "123", "role": "user"}
}

TOKENS_DB: Dict[str, str] = {}
TASKS_DB: Dict[str, dict] = {}
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")

def get_current_user(token: str = Depends(oauth2_scheme)):
    if token not in TOKENS_DB:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="身份认证已过期，请重新登录")
    return USERS_DB[TOKENS_DB[token]]

# ---------------- 2. 图像处理核心逻辑 ----------------
def find_and_modify_height(data, original_height):
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
                else:
                    return None
            else:
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

def remove_eoi(data):
    for i in range(len(data) - 1, 0, -1):
        if data[i-1] == 0xFF and data[i] == 0xD9:
            return data[:i-1] + data[i+1:], True
    return data, False

def background_process_image(task_id: str, input_path: str, output_path: str, junk_size_mb: float):
    task = TASKS_DB[task_id]
    task["status"] = "处理中"
    temp_img_path = input_path + ".tmp.jpg"
    
    try:
        img = Image.open(input_path)
        width, height = img.size
        original_height = height
        
        if height <= 2:
            task["status"] = "失败：图片高度不足 2 行"
            return

        new_height = height - 2
        cropped_img = img.crop((0, 0, width, new_height))
        img.close()
        
        cropped_img.save(temp_img_path, "JPEG", quality=95)
        cropped_img.close()
        
        if task["stop_requested"]:
            task["status"] = "已取消/停止"
            return
            
        with open(temp_img_path, "rb") as f:
            image_data = bytearray(f.read())

        modified_data = find_and_modify_height(image_data, original_height)
        if modified_data is None:
            modified_data = image_data
            
        modified_data, _ = remove_eoi(modified_data)
        
        junk_size_bytes = int(junk_size_mb * 1024 * 1024)
        chunk_size = 1024 * 512
        added = 0
        
        while added < junk_size_bytes:
            if task["stop_requested"]:
                task["status"] = "已取消/停止"
                return
            to_add = min(chunk_size, junk_size_bytes - added)
            modified_data += bytearray(random.getrandbits(8) for _ in range(to_add))
            added += to_add
            time.sleep(0.01)
            
        with open(output_path, "wb") as f:
            f.write(modified_data)
            
        task["status"] = "已完成"
        task["result_path"] = output_path
    except Exception as e:
        task["status"] = f"失败: {str(e)}"
    finally:
        if os.path.exists(input_path):
            os.remove(input_path)
        if os.path.exists(temp_img_path):
            os.remove(temp_img_path)

# ---------------- 3. API 路由 ----------------
@app.post("/api/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = USERS_DB.get(form_data.username)
    if not user or user["password"] != form_data.password:
        raise HTTPException(status_code=400, detail="用户名或密码错误")
    token = str(uuid.uuid4())
    TOKENS_DB[token] = user["username"]
    return {"access_token": token, "token_type": "bearer", "role": user["role"], "username": user["username"]}

@app.post("/api/admin/kickout")
async def kickout_user(username: str = Form(...), current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="无权限操作")
    if username == "admin":
        raise HTTPException(status_code=400, detail="不能下线管理员自己")
    
    tokens_to_remove = [token for token, user in TOKENS_DB.items() if user == username]
    for token in tokens_to_remove:
        del TOKENS_DB[token]
    return {"message": f"已成功将用户 {username} 强制下线"}

@app.get("/api/admin/online-users")
async def get_online_users(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="无权限访问此数据")
    return {"online_users": list(set(TOKENS_DB.values()))}

@app.post("/api/tasks/create")
async def create_task(file: UploadFile = File(...), junk_size_mb: float = Form(1.0), current_user: dict = Depends(get_current_user)):
    task_id = str(uuid.uuid4())[:8]
    temp_dir = "/tmp/tasks"
    os.makedirs(temp_dir, exist_ok=True)
    
    input_path = os.path.join(temp_dir, f"in_{task_id}_{file.filename}")
    base_name, _ = os.path.splitext(file.filename)
    output_path = os.path.join(temp_dir, f"{base_name}_{task_id}.file")
    
    with open(input_path, "wb") as buffer:
        buffer.write(await file.read())
        
    TASKS_DB[task_id] = {
        "task_id": task_id, "username": current_user["username"], "filename": file.filename,
        "status": "排队中", "stop_requested": False, "result_path": None, "created_at": time.strftime("%H:%M:%S")
    }
    
    t = threading.Thread(target=background_process_image, args=(task_id, input_path, output_path, junk_size_mb))
    t.start()
    return {"message": "任务已创建", "task_id": task_id}

@app.get("/api/tasks")
async def list_tasks(current_user: dict = Depends(get_current_user)):
    if current_user["role"] == "admin":
        return list(TASKS_DB.values())
    return [t for t in TASKS_DB.values() if t["username"] == current_user["username"]]

@app.post("/api/tasks/{task_id}/stop")
async def stop_task(task_id: str, current_user: dict = Depends(get_current_user)):
    task = TASKS_DB.get(task_id)
    if not task: raise HTTPException(status_code=404, detail="任务不存在")
    if current_user["role"] != "admin" and task["username"] != current_user["username"]:
        raise HTTPException(status_code=403, detail="无权限")
    task["stop_requested"] = True
    task["status"] = "正在停止..."
    return {"message": "已发送停止信号"}

@app.get("/api/tasks/{task_id}/download")
async def download_task_file(task_id: str, current_user: dict = Depends(get_current_user)):
    task = TASKS_DB.get(task_id)
    if not task or task["status"] != "已完成":
        raise HTTPException(status_code=400, detail="文件尚未就绪")
    if current_user["role"] != "admin" and task["username"] != current_user["username"]:
        raise HTTPException(status_code=403, detail="无权限")
    base_name, _ = os.path.splitext(task["filename"])
    return FileResponse(task["result_path"], filename=f"{base_name}.file")

# ---------------- 4. 前端页面 ----------------
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <title>在线图片处理系统</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 30px; background: #f4f4f9; }
            .card { background: white; padding: 25px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); max-width: 900px; margin-left: auto; margin-right: auto; }
            .hidden { display: none !important; }
            input[type="text"], input[type="password"] { padding: 8px 12px; border: 1px solid #ccc; border-radius: 4px; margin-right: 10px; }
            table { width: 100%; border-collapse: collapse; margin-top: 15px; }
            th, td { border: 1px solid #eee; padding: 10px; text-align: left; }
            th { background-color: #f8f9fa; }
            .btn { padding: 6px 14px; border: none; border-radius: 4px; cursor: pointer; font-weight: 500; }
            .btn-primary { background-color: #0066cc; color: white; }
            .btn-danger { background-color: #dc3545; color: white; }
            .btn-success { background-color: #28a745; color: white; }
        </style>
    </head>
    <body>
        <div id="loginCard" class="card">
            <h2>系统登录</h2>
            <div style="margin-top: 15px;">
                <input type="text" id="loginUsername" placeholder="请输入用户名">
                <input type="password" id="loginPassword" placeholder="请输入密码">
                <button class="btn btn-primary" onclick="login()">登录</button>
            </div>
        </div>

        <div id="mainCard" class="card hidden">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <h2>控制台 - 当前用户: <span id="userInfo"></span></h2>
                <button class="btn btn-danger" onclick="logout()">退出登录</button>
            </div>
            <hr style="border:0; border-top:1px solid #eee; margin: 15px 0;">
            
            <h3>上传 JPEG 图片</h3>
            <div>
                <input type="file" id="imageFile" accept=".jpg,.jpeg,.jpe,.jfif">
                垃圾数据 (MB): <input type="number" id="junkSize" value="1.0" step="0.5" style="width:60px;">
                <button class="btn btn-primary" onclick="submitTask()">开始处理</button>
            </div>

            <h3 style="margin-top: 30px;">任务列表</h3>
            <table>
                <thead>
                    <tr><th>任务 ID</th><th>所属用户</th><th>文件名</th><th>状态</th><th>创建时间</th><th>操作</th></tr>
                </thead>
                <tbody id="taskTableBody"></tbody>
            </table>

            <div id="adminPanel" class="hidden" style="margin-top: 30px; border-top: 2px dashed #eee; padding-top: 15px;">
                <h3>在线用户管理（管理员专属）</h3>
                <ul id="onlineUsersList" style="padding-left: 20px;"></ul>
            </div>
        </div>

        <script>
            let currentUser = null, pollInterval = null;

            window.onload = () => {
                const token = localStorage.getItem("token");
                if (token) {
                    currentUser = {
                        token,
                        role: localStorage.getItem("role"),
                        username: localStorage.getItem("username")
                    };
                    showMainUI();
                }
            };

            async function login() {
                const u = document.getElementById("loginUsername").value;
                const p = document.getElementById("loginPassword").value;
                if(!u || !p) return alert("请输入用户名和密码");

                const body = new URLSearchParams({ username: u, password: p });
                const res = await fetch('/api/login', { method: "POST", body });
                const data = await res.json();
                
                if (!res.ok) return alert("登录失败: " + data.detail);

                currentUser = { token: data.access_token, role: data.role, username: data.username };
                localStorage.setItem("token", data.access_token);
                localStorage.setItem("role", data.role);
                localStorage.setItem("username", data.username);
                showMainUI();
            }

            function logout() {
                localStorage.clear();
                currentUser = null;
                clearInterval(pollInterval);
                document.getElementById("loginCard").classList.remove("hidden");
                document.getElementById("mainCard").classList.add("hidden");
                document.getElementById("adminPanel").classList.add("hidden");
            }

            function showMainUI() {
                document.getElementById("loginCard").classList.add("hidden");
                document.getElementById("mainCard").classList.remove("hidden");
                document.getElementById("userInfo").innerText = `${currentUser.username} (${currentUser.role === 'admin' ? '管理员' : '普通用户'})`;
                
                // 只有角色的确是 admin 时才显示在线管理区域并拉取在线列表
                if (currentUser.role === 'admin') {
                    document.getElementById("adminPanel").classList.remove("hidden");
                    fetchOnlineUsers();
                } else {
                    document.getElementById("adminPanel").classList.add("hidden");
                }

                fetchTasks();
                clearInterval(pollInterval);
                pollInterval = setInterval(() => {
                    fetchTasks();
                    if (currentUser && currentUser.role === 'admin') {
                        fetchOnlineUsers();
                    }
                }, 2000);
            }

            async function submitTask() {
                const file = document.getElementById("imageFile").files[0];
                if (!file) return alert("请选择要处理的图片文件！");
                const formData = new FormData();
                formData.append("file", file);
                formData.append("junk_size_mb", document.getElementById("junkSize").value);

                const res = await fetch('/api/tasks/create', {
                    method: "POST",
                    headers: { "Authorization": `Bearer ${currentUser.token}` },
                    body: formData
                });
                if (res.ok) { fetchTasks(); } else { alert("提交失败"); }
            }

            async function fetchTasks() {
                if (!currentUser) return;
                const res = await fetch('/api/tasks', { headers: { "Authorization": `Bearer ${currentUser.token}` } });
                if (!res.ok) {
                    if(res.status === 401) logout();
                    return;
                }
                const tasks = await res.json();
                const tbody = document.getElementById("taskTableBody");
                tbody.innerHTML = "";
                tasks.forEach(t => {
                    const isOwner = t.username === currentUser.username, isAdmin = currentUser.role === "admin";
                    const isRunning = t.status === "排队中" || t.status === "处理中";
                    let btn = "";
                    if (isRunning && (isAdmin || isOwner)) {
                        btn += `<button class="btn btn-danger" onclick="stopTask('${t.task_id}')">停止</button> `;
                    }
                    if (t.status === "已完成") {
                        btn += `<button class="btn btn-success" onclick="downloadFile('${t.task_id}')">下载结果</button>`;
                    }
                    tbody.innerHTML += `<tr><td>${t.task_id}</td><td><b>${t.username}</b></td><td>${t.filename}</td><td>${t.status}</td><td>${t.created_at}</td><td>${btn}</td></tr>`;
                });
            }

            async function fetchOnlineUsers() {
                if (!currentUser || currentUser.role !== 'admin') return;
                const res = await fetch('/api/admin/online-users', { headers: { "Authorization": `Bearer ${currentUser.token}` } });
                if (!res.ok) return;
                const data = await res.json();
                const list = document.getElementById("onlineUsersList");
                list.innerHTML = "";
                data.online_users.forEach(user => {
                    if(user !== 'admin') {
                        list.innerHTML += `<li style="margin-bottom:8px;">用户：<b>${user}</b> <button class="btn btn-danger" style="padding:2px 8px; font-size:12px;" onclick="kickout('${user}')">强行下线</button></li>`;
                    }
                });
            }

            async function kickout(username) {
                if(!confirm(`确定要强行踢出用户 ${username} 吗？`)) return;
                const formData = new FormData();
                formData.append("username", username);
                const res = await fetch('/api/admin/kickout', {
                    method: "POST",
                    headers: { "Authorization": `Bearer ${currentUser.token}` },
                    body: formData
                });
                const data = await res.json();
                alert(data.message);
                fetchOnlineUsers();
            }

            async function stopTask(id) {
                await fetch(`/api/tasks/${id}/stop`, { method: "POST", headers: { "Authorization": `Bearer ${currentUser.token}` } });
                fetchTasks();
            }

            async function downloadFile(id) {
                const res = await fetch(`/api/tasks/${id}/download`, { headers: { "Authorization": `Bearer ${currentUser.token}` } });
                const blob = await res.blob();
                const a = document.createElement('a');
                a.href = window.URL.createObjectURL(blob);
                a.download = `${id}.file`;
                a.click();
            }
        </script>
    </body>
    </html>
    """

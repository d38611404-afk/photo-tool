import os
import random
import time
import uuid
import threading
import zipfile
from typing import Dict, List
from fastapi import FastAPI, File, UploadFile, Form, Depends, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from PIL import Image

app = FastAPI(title="在线图片处理系统 - 设备锁死版")

# ---------------- 1. 用户与任务数据库 ----------------
# 增加了 bound_device 字段，初始为 None（未绑定）
USERS_DB = {
    "admin": {"username": "admin", "password": "adminpassword", "role": "admin", "bound_device": None},
    "user1": {"username": "user1", "password": "123", "role": "user", "bound_device": None},
    "user2": {"username": "user2", "password": "123", "role": "user", "bound_device": None}
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
            task["status"] = "失败：高度不足"
            return

        new_height = height - 2
        cropped_img = img.crop((0, 0, width, new_height))
        img.close()
        
        cropped_img.save(temp_img_path, "JPEG", quality=95)
        cropped_img.close()
        
        if task["stop_requested"]:
            task["status"] = "已取消"
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
                task["status"] = "已取消"
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

# 登录逻辑：包含设备识别与设备锁死校验
@app.post("/api/login")
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    device_id: str = Form(...)
):
    user = USERS_DB.get(form_data.username)
    if not user or user["password"] != form_data.password:
        raise HTTPException(status_code=400, detail="用户名或密码错误")
    
    # 核心：判断设备绑定锁
    if user["bound_device"] is None:
        # 首次登录，自动绑定当前电脑指纹
        user["bound_device"] = device_id
    elif user["bound_device"] != device_id:
        # 绑定的指纹与当前电脑不符，拒绝登录
        raise HTTPException(
            status_code=403, 
            detail="【拒绝登录】该账号已锁死绑定在其他电脑上，无法在此电脑使用！"
        )
        
    token = str(uuid.uuid4())
    TOKENS_DB[token] = user["username"]
    return {"access_token": token, "token_type": "bearer", "role": user["role"], "username": user["username"]}

# 管理员专属：一键解绑某个用户的设备锁
@app.post("/api/admin/unbind_device")
async def unbind_device(username: str = Form(...), current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="无权限操作")
    
    user = USERS_DB.get(username)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
        
    user["bound_device"] = None  # 重置绑定的设备 ID
    return {"message": f"已成功解锁用户 {username} 的设备绑定！下次登录将绑定新电脑。"}

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
        raise HTTPException(status_code=403, detail="无权限")
    return {"online_users": list(set(TOKENS_DB.values()))}

@app.post("/api/tasks/create_batch")
async def create_batch_tasks(files: List[UploadFile] = File(...), junk_size_mb: float = Form(1.0), current_user: dict = Depends(get_current_user)):
    task_ids = []
    temp_dir = "/tmp/tasks"
    os.makedirs(temp_dir, exist_ok=True)
    
    for file in files:
        task_id = str(uuid.uuid4())[:8]
        input_path = os.path.join(temp_dir, f"in_{task_id}_{file.filename}")
        base_name, _ = os.path.splitext(file.filename)
        output_path = os.path.join(temp_dir, f"{base_name}_{task_id}.file")
        
        with open(input_path, "wb") as buffer:
            buffer.write(await file.read())
            
        TASKS_DB[task_id] = {
            "task_id": task_id, "username": current_user["username"], "filename": file.filename,
            "out_name": f"{base_name}.file",
            "status": "排队中", "stop_requested": False, "result_path": None, "created_at": time.strftime("%H:%M:%S")
        }
        
        t = threading.Thread(target=background_process_image, args=(task_id, input_path, output_path, junk_size_mb))
        t.start()
        task_ids.append(task_id)
        
    return {"message": f"已成功提交 {len(task_ids)} 个文件处理", "task_ids": task_ids}

@app.get("/api/tasks")
async def list_tasks(current_user: dict = Depends(get_current_user)):
    if current_user["role"] == "admin":
        return list(TASKS_DB.values())
    return [t for t in TASKS_DB.values() if t["username"] == current_user["username"]]

@app.get("/api/tasks/download_zip")
async def download_zip(current_user: dict = Depends(get_current_user)):
    user_tasks = [t for t in TASKS_DB.values() if (current_user["role"] == "admin" or t["username"] == current_user["username"]) and t["status"] == "已完成"]
    if not user_tasks:
        raise HTTPException(status_code=400, detail="当前没有已完成的可供下载的文件")
        
    zip_filename = f"processed_files_{int(time.time())}.zip"
    zip_path = os.path.join("/tmp", zip_filename)
    
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for t in user_tasks:
            if t["result_path"] and os.path.exists(t["result_path"]):
                zipf.write(t["result_path"], arcname=t["out_name"])
                
    return FileResponse(zip_path, filename="批量处理图片包.zip")

@app.post("/api/tasks/clear")
async def clear_tasks(current_user: dict = Depends(get_current_user)):
    to_delete = []
    for task_id, task in TASKS_DB.items():
        if current_user["role"] == "admin" or task["username"] == current_user["username"]:
            if task["result_path"] and os.path.exists(task["result_path"]):
                try: os.remove(task["result_path"])
                except: pass
            to_delete.append(task_id)
            
    for task_id in to_delete:
        del TASKS_DB[task_id]
        
    return {"message": f"已成功清空 {len(to_delete)} 个任务记录"}

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
    return FileResponse(task["result_path"], filename=task["out_name"])

# ---------------- 4. 前端页面 ----------------
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <title>在线图片处理系统 - 设备锁死版</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 30px; background: #f4f4f9; }
            .card { background: white; padding: 25px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); max-width: 950px; margin-left: auto; margin-right: auto; }
            .hidden { display: none !important; }
            input[type="text"], input[type="password"] { padding: 8px 12px; border: 1px solid #ccc; border-radius: 4px; margin-right: 10px; }
            table { width: 100%; border-collapse: collapse; margin-top: 15px; }
            th, td { border: 1px solid #eee; padding: 10px; text-align: left; }
            th { background-color: #f8f9fa; }
            .btn { padding: 6px 14px; border: none; border-radius: 4px; cursor: pointer; font-weight: 500; margin-right: 5px; }
            .btn-primary { background-color: #0066cc; color: white; }
            .btn-danger { background-color: #dc3545; color: white; }
            .btn-success { background-color: #28a745; color: white; }
            .btn-warning { background-color: #ff9800; color: white; }
            .toolbar { display: flex; justify-content: space-between; align-items: center; margin-top: 15px; }
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
            
            <h3>批量上传 JPEG 图片</h3>
            <div>
                <input type="file" id="imageFiles" accept=".jpg,.jpeg,.jpe,.jfif" multiple>
                垃圾数据 (MB): <input type="number" id="junkSize" value="1.0" step="0.5" style="width:60px;">
                <button class="btn btn-primary" onclick="submitBatchTasks()">开始批量处理</button>
            </div>

            <div class="toolbar" style="margin-top: 25px;">
                <h3 style="margin: 0;">任务列表</h3>
                <div>
                    <button class="btn btn-success" onclick="downloadZip()">📦 打包下载全部已完成 (.zip)</button>
                    <button class="btn btn-warning" onclick="clearTasks()">🧹 刷新/清除所有记录</button>
                </div>
            </div>

            <table>
                <thead>
                    <tr><th>任务 ID</th><th>所属用户</th><th>文件名</th><th>状态</th><th>创建时间</th><th>操作</th></tr>
                </thead>
                <tbody id="taskTableBody"></tbody>
            </table>

            <div id="adminPanel" class="hidden" style="margin-top: 30px; border-top: 2px dashed #eee; padding-top: 15px;">
                <h3>在线用户及设备锁管理（管理员专属）</h3>
                <ul id="onlineUsersList" style="padding-left: 20px;"></ul>
            </div>
        </div>

        <script>
            let currentUser = null, pollInterval = null;

            // 生成/获取当前电脑的唯一硬件设备指纹
            function getDeviceId() {
                let deviceId = localStorage.getItem("system_device_fingerprint");
                if (!deviceId) {
                    deviceId = 'DEV-' + Math.random().toString(36).substring(2, 15) + '-' + new Date().getTime();
                    localStorage.setItem("system_device_fingerprint", deviceId);
                }
                return deviceId;
            }

            window.onload = () => {
                const token = localStorage.getItem("token");
                if (token) {
                    currentUser = { token, role: localStorage.getItem("role"), username: localStorage.getItem("username") };
                    showMainUI();
                }
            };

            async function login() {
                const u = document.getElementById("loginUsername").value;
                const p = document.getElementById("loginPassword").value;
                if(!u || !p) return alert("请输入用户名和密码");

                const deviceId = getDeviceId();
                const body = new URLSearchParams({ 
                    username: u, 
                    password: p,
                    device_id: deviceId 
                });

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
                localStorage.clear(); currentUser = null; clearInterval(pollInterval);
                document.getElementById("loginCard").classList.remove("hidden");
                document.getElementById("mainCard").classList.add("hidden");
                document.getElementById("adminPanel").classList.add("hidden");
            }

            function showMainUI() {
                document.getElementById("loginCard").classList.add("hidden");
                document.getElementById("mainCard").classList.remove("hidden");
                document.getElementById("userInfo").innerText = `${currentUser.username} (${currentUser.role === 'admin' ? '管理员' : '普通用户'})`;
                
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
                    if (currentUser && currentUser.role === 'admin') fetchOnlineUsers();
                }, 2000);
            }

            async function submitBatchTasks() {
                const files = document.getElementById("imageFiles").files;
                if (!files || files.length === 0) return alert("请至少选择一张图片！");

                const formData = new FormData();
                for(let i = 0; i < files.length; i++) {
                    formData.append("files", files[i]);
                }
                formData.append("junk_size_mb", document.getElementById("junkSize").value);

                const res = await fetch('/api/tasks/create_batch', {
                    method: "POST",
                    headers: { "Authorization": `Bearer ${currentUser.token}` },
                    body: formData
                });
                if (res.ok) { 
                    alert(`成功提交 ${files.length} 张图片！`);
                    document.getElementById("imageFiles").value = "";
                    fetchTasks(); 
                } else { alert("提交失败"); }
            }

            async function fetchTasks() {
                if (!currentUser) return;
                const res = await fetch('/api/tasks', { headers: { "Authorization": `Bearer ${currentUser.token}` } });
                if (!res.ok) { if(res.status === 401) logout(); return; }
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
                        btn += `<button class="btn btn-success" onclick="downloadFile('${t.task_id}')">单文件下载</button>`;
                    }
                    tbody.innerHTML += `<tr><td>${t.task_id}</td><td><b>${t.username}</b></td><td>${t.filename}</td><td>${t.status}</td><td>${t.created_at}</td><td>${btn}</td></tr>`;
                });
            }

            async function downloadZip() {
                const res = await fetch('/api/tasks/download_zip', { headers: { "Authorization": `Bearer ${currentUser.token}` } });
                if(!res.ok) {
                    const err = await res.json();
                    return alert(err.detail || "下载失败");
                }
                const blob = await res.blob();
                const a = document.createElement('a');
                a.href = window.URL.createObjectURL(blob);
                a.download = `批量图片包_${new Date().getTime()}.zip`;
                a.click();
            }

            async function clearTasks() {
                if(!confirm("确定要刷新并清除当前列表及服务器上的所有临时文件吗？")) return;
                const res = await fetch('/api/tasks/clear', {
                    method: "POST",
                    headers: { "Authorization": `Bearer ${currentUser.token}` }
                });
                if(res.ok) {
                    alert("已经成功清除！");
                    fetchTasks();
                }
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
                        list.innerHTML += `<li style="margin-bottom:8px;">
                            用户：<b>${user}</b> 
                            <button class="btn btn-danger" style="padding:2px 8px; font-size:12px;" onclick="kickout('${user}')">强行下线</button>
                            <button class="btn btn-warning" style="padding:2px 8px; font-size:12px;" onclick="unbindDevice('${user}')">🔓 重置/解锁设备绑定</button>
                        </li>`;
                    }
                });
            }

            async function unbindDevice(username) {
                if(!confirm(`确定要解锁用户 ${username} 的电脑绑定吗？解锁后，该用户下次在新的电脑登录时将重新绑定新电脑。`)) return;
                const formData = new FormData();
                formData.append("username", username);
                const res = await fetch('/api/admin/unbind_device', {
                    method: "POST",
                    headers: { "Authorization": `Bearer ${currentUser.token}` },
                    body: formData
                });
                const data = await res.json();
                alert(data.message);
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

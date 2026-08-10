import os
import time
import uuid
import threading
import zipfile
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from fastapi import FastAPI, File, UploadFile, Form, Depends, HTTPException, status, Header
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from PIL import Image

app = FastAPI(title="在线图片处理系统 - 隔离极速下载版")

# ---------------- 1. 用户与任务数据库 ----------------
USERS_DB = {
    "admin": {"username": "admin", "password": "adminpassword", "role": "admin", "bound_device": None},
    "user1": {"username": "user1", "password": "123", "role": "user", "bound_device": None},
    "user2": {"username": "user2", "password": "123", "role": "user", "bound_device": None}
}

TOKENS_DB: Dict[str, str] = {}
TASKS_DB: Dict[str, dict] = {}
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login", auto_error=False)

def get_user_from_request(
    token: Optional[str] = None, 
    auth_header: Optional[str] = Header(None, alias="Authorization")
):
    valid_token = None
    if token and token in TOKENS_DB:
        valid_token = token
    elif auth_header and auth_header.startswith("Bearer "):
        header_token = auth_header.split(" ")[1]
        if header_token in TOKENS_DB:
            valid_token = header_token

    if not valid_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="身份认证已过期或无效，请重新登录"
        )
    return USERS_DB[TOKENS_DB[valid_token]]

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
            modified_data += os.urandom(to_add)
            added += to_add
            
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
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    device_id: str = Form(...)
):
    user = USERS_DB.get(form_data.username)
    if not user or user["password"] != form_data.password:
        raise HTTPException(status_code=400, detail="用户名或密码错误")
    
    if user["role"] != "admin":
        if user["bound_device"] is None:
            user["bound_device"] = device_id
        elif user["bound_device"] != device_id:
            raise HTTPException(
                status_code=403, 
                detail="【拒绝登录】该账号已锁死绑定在其他电脑上，无法在此电脑使用！"
            )
        
    token = str(uuid.uuid4())
    TOKENS_DB[token] = user["username"]
    return {"access_token": token, "token_type": "bearer", "role": user["role"], "username": user["username"]}

@app.get("/api/admin/users")
async def list_all_users(current_user: dict = Depends(get_user_from_request)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="无权限")
    result = []
    for u, data in USERS_DB.items():
        result.append({
            "username": data["username"],
            "role": data["role"],
            "is_bound": data["bound_device"] is not None
        })
    return result

@app.post("/api/admin/save_user")
async def save_or_update_user(
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
    current_user: dict = Depends(get_user_from_request)
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="无权限操作")
    
    if username in USERS_DB:
        USERS_DB[username]["password"] = password
        USERS_DB[username]["role"] = role
        msg = f"账号 {username} 信息更新成功！"
    else:
        USERS_DB[username] = {
            "username": username, "password": password, "role": role, "bound_device": None
        }
        msg = f"账号 {username} 添加成功！"
        
    return {"message": msg}

@app.post("/api/admin/delete_user")
async def delete_user(username: str = Form(...), current_user: dict = Depends(get_user_from_request)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="无权限操作")
    if username == "admin":
        raise HTTPException(status_code=400, detail="不能删除系统内置管理账号 admin")
    if username in USERS_DB:
        del USERS_DB[username]
        tokens_to_remove = [token for token, user in TOKENS_DB.items() if user == username]
        for token in tokens_to_remove:
            del TOKENS_DB[token]
        return {"message": f"用户 {username} 已成功删除！"}
    raise HTTPException(status_code=404, detail="用户不存在")

@app.post("/api/admin/unbind_device")
async def unbind_device(username: str = Form(...), current_user: dict = Depends(get_user_from_request)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="无权限操作")
    user = USERS_DB.get(username)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    user["bound_device"] = None
    return {"message": f"已成功解锁用户 {username} 的设备绑定！"}

@app.post("/api/admin/kickout")
async def kickout_user(username: str = Form(...), current_user: dict = Depends(get_user_from_request)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="无权限操作")
    if username == "admin":
        raise HTTPException(status_code=400, detail="不能下线管理员自己")
    tokens_to_remove = [token for token, user in TOKENS_DB.items() if user == username]
    for token in tokens_to_remove:
        del TOKENS_DB[token]
    return {"message": f"已成功将用户 {username} 强制下线"}

@app.get("/api/admin/online-users")
async def get_online_users(current_user: dict = Depends(get_user_from_request)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="无权限")
    return {"online_users": list(set(TOKENS_DB.values()))}

@app.post("/api/tasks/create_batch")
async def create_batch_tasks(files: List[UploadFile] = File(...), junk_size_mb: float = Form(1.0), current_user: dict = Depends(get_user_from_request)):
    task_ids = []
    temp_dir = "/tmp/tasks"
    os.makedirs(temp_dir, exist_ok=True)
    
    beijing_tz = timezone(timedelta(hours=8))
    
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
            "status": "排队中", "stop_requested": False, "result_path": None, 
            "created_at": datetime.now(beijing_tz).strftime("%H:%M:%S")
        }
        
        t = threading.Thread(target=background_process_image, args=(task_id, input_path, output_path, junk_size_mb))
        t.start()
        task_ids.append(task_id)
        
    return {"message": f"已成功提交 {len(task_ids)} 个文件处理", "task_ids": task_ids}

# 【优化1：每个人（包含管理员）都只看到自己上传的任务，彻底解决混乱】
@app.get("/api/tasks")
async def list_tasks(current_user: dict = Depends(get_user_from_request)):
    return [t for t in TASKS_DB.values() if t["username"] == current_user["username"]]

# 【优化2：改用 zipfile.ZIP_STORED 模式，零CPU压缩开销，极速秒打包】
@app.get("/api/tasks/download_zip")
async def download_zip(current_user: dict = Depends(get_user_from_request)):
    user_tasks = [t for t in TASKS_DB.values() if t["username"] == current_user["username"] and t["status"] == "已完成"]
    if not user_tasks:
        raise HTTPException(status_code=400, detail="当前没有已完成的可供下载的文件")
        
    zip_filename = f"processed_files_{int(time.time())}.zip"
    zip_path = os.path.join("/tmp", zip_filename)
    
    # 使用 ZIP_STORED (只归档不压缩)，提速 5-10 倍
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_STORED) as zipf:
        for t in user_tasks:
            if t["result_path"] and os.path.exists(t["result_path"]):
                zipf.write(t["result_path"], arcname=t["out_name"])
                
    return FileResponse(zip_path, filename="批量处理图片包.zip")

@app.post("/api/tasks/clear")
async def clear_tasks(current_user: dict = Depends(get_user_from_request)):
    to_delete = []
    for task_id, task in TASKS_DB.items():
        if task["username"] == current_user["username"]:
            if task["result_path"] and os.path.exists(task["result_path"]):
                try: os.remove(task["result_path"])
                except: pass
            to_delete.append(task_id)
            
    for task_id in to_delete:
        del TASKS_DB[task_id]
        
    return {"message": f"已成功清空 {len(to_delete)} 个任务记录"}

@app.post("/api/tasks/{task_id}/stop")
async def stop_task(task_id: str, current_user: dict = Depends(get_user_from_request)):
    task = TASKS_DB.get(task_id)
    if not task: raise HTTPException(status_code=404, detail="任务不存在")
    if task["username"] != current_user["username"]:
        raise HTTPException(status_code=403, detail="无权限操作他人任务")
    task["stop_requested"] = True
    task["status"] = "正在停止..."
    return {"message": "已发送停止信号"}

@app.get("/api/tasks/{task_id}/download")
async def download_task_file(task_id: str, current_user: dict = Depends(get_user_from_request)):
    task = TASKS_DB.get(task_id)
    if not task or task["status"] != "已完成":
        raise HTTPException(status_code=400, detail="文件尚未就绪")
    if task["username"] != current_user["username"]:
        raise HTTPException(status_code=403, detail="无权限下载他人文件")
    return FileResponse(task["result_path"], filename=task["out_name"])

# ---------------- 4. 前端页面 ----------------
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <title>在线图片处理系统 - 极速隔离版</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 30px; background: #f4f4f9; }
            .card { background: white; padding: 25px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); max-width: 950px; margin-left: auto; margin-right: auto; }
            .hidden { display: none !important; }
            input[type="text"], input[type="password"], select { padding: 8px 12px; border: 1px solid #ccc; border-radius: 4px; margin-right: 10px; }
            table { width: 100%; border-collapse: collapse; margin-top: 15px; }
            th, td { border: 1px solid #eee; padding: 10px; text-align: left; }
            th { background-color: #f8f9fa; }
            .btn { padding: 6px 14px; border: none; border-radius: 4px; cursor: pointer; font-weight: 500; margin-right: 5px; }
            .btn-primary { background-color: #0066cc; color: white; }
            .btn-danger { background-color: #dc3545; color: white; }
            .btn-success { background-color: #28a745; color: white; }
            .btn-warning { background-color: #ff9800; color: white; }
            .btn:disabled { background-color: #cccccc; cursor: not-allowed; }
            .toolbar { display: flex; justify-content: space-between; align-items: center; margin-top: 15px; }
            .form-box { background: #f9f9f9; padding: 15px; border-radius: 6px; margin-bottom: 15px; border: 1px dashed #ddd; }
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
                <button id="submitBtn" class="btn btn-primary" onclick="submitBatchTasks()">开始批量处理</button>
            </div>

            <div class="toolbar" style="margin-top: 25px;">
                <h3 style="margin: 0;">我的任务列表 (处理完成自动打包下载)</h3>
                <div>
                    <button id="zipBtn" class="btn btn-success" onclick="downloadZip()">📦 打包下载全部已完成 (.zip)</button>
                    <button class="btn btn-warning" onclick="clearTasks()">🧹 刷新/清除我的记录</button>
                </div>
            </div>

            <table>
                <thead>
                    <tr><th>任务 ID</th><th>所属用户</th><th>文件名</th><th>状态</th><th>创建时间</th><th>操作</th></tr>
                </thead>
                <tbody id="taskTableBody"></tbody>
            </table>

            <div id="adminPanel" class="hidden" style="margin-top: 30px; border-top: 2px dashed #0066cc; padding-top: 15px;">
                <h3 style="color: #0066cc;">账号与设备锁管理（管理员专属面板）</h3>
                <div class="form-box">
                    <h4 style="margin-top: 0;">➕ 添加或修改账号密码</h4>
                    <input type="text" id="newUsername" placeholder="用户名">
                    <input type="password" id="newPassword" placeholder="设置密码">
                    <select id="newRole">
                        <option value="user">普通用户 (受设备限制)</option>
                        <option value="admin">管理员 (无设备限制)</option>
                    </select>
                    <button class="btn btn-primary" onclick="saveUser()">保存账号</button>
                </div>
                <h4>系统已存账号列表</h4>
                <table>
                    <thead>
                        <tr><th>用户名</th><th>角色</th><th>设备绑定状态</th><th>操作</th></tr>
                    </thead>
                    <tbody id="userTableBody"></tbody>
                </table>
                <h4 style="margin-top: 20px;">当前在线用户</h4>
                <ul id="onlineUsersList" style="padding-left: 20px;"></ul>
            </div>
        </div>

        <script>
            let currentUser = null, pollInterval = null;
            let hasAutoDownloadedZip = false; 

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
                const body = new URLSearchParams({ username: u, password: p, device_id: deviceId });

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
                    fetchAdminData();
                } else {
                    document.getElementById("adminPanel").classList.add("hidden");
                }

                fetchTasks();
                clearInterval(pollInterval);
                pollInterval = setInterval(() => {
                    fetchTasks();
                    if (currentUser && currentUser.role === 'admin') fetchAdminData();
                }, 2000);
            }

            async function fetchAdminData() {
                fetchOnlineUsers();
                fetchUserList();
            }

            async function fetchUserList() {
                if (!currentUser || currentUser.role !== 'admin') return;
                const res = await fetch('/api/admin/users', { headers: { "Authorization": `Bearer ${currentUser.token}` } });
                if (!res.ok) return;
                const users = await res.json();
                const tbody = document.getElementById("userTableBody");
                tbody.innerHTML = "";
                users.forEach(u => {
                    let actions = "";
                    if(u.is_bound) {
                        actions += `<button class="btn btn-warning" style="padding:2px 8px; font-size:12px;" onclick="unbindDevice('${u.username}')">🔓 重置设备锁</button> `;
                    }
                    if(u.username !== 'admin') {
                        actions += `<button class="btn btn-danger" style="padding:2px 8px; font-size:12px;" onclick="deleteUser('${u.username}')">🗑️ 删除账户</button>`;
                    }
                    tbody.innerHTML += `<tr>
                        <td><b>${u.username}</b></td>
                        <td>${u.role === 'admin' ? '<span style="color:red">管理员</span>' : '普通用户'}</td>
                        <td>${u.is_bound ? '<span style="color:orange">🔒 已绑定设备</span>' : '<span style="color:green">🔓 未绑定 (可新登)</span>'}</td>
                        <td>${actions}</td>
                    </tr>`;
                });
            }

            async function saveUser() {
                const u = document.getElementById("newUsername").value;
                const p = document.getElementById("newPassword").value;
                const r = document.getElementById("newRole").value;
                if(!u || !p) return alert("请填写完整的用户名和密码！");

                const formData = new FormData();
                formData.append("username", u);
                formData.append("password", p);
                formData.append("role", r);

                const res = await fetch('/api/admin/save_user', { method: "POST", headers: { "Authorization": `Bearer ${currentUser.token}` }, body: formData });
                const data = await res.json();
                alert(data.message);
                document.getElementById("newUsername").value = "";
                document.getElementById("newPassword").value = "";
                fetchUserList();
            }

            async function deleteUser(username) {
                if(!confirm(`确定要彻底删除账号 ${username} 吗？`)) return;
                const formData = new FormData();
                formData.append("username", username);
                const res = await fetch('/api/admin/delete_user', { method: "POST", headers: { "Authorization": `Bearer ${currentUser.token}` }, body: formData });
                const data = await res.json();
                alert(data.message);
                fetchUserList();
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

            async function unbindDevice(username) {
                if(!confirm(`确定要解锁用户 ${username} 的电脑绑定吗？`)) return;
                const formData = new FormData();
                formData.append("username", username);
                const res = await fetch('/api/admin/unbind_device', { method: "POST", headers: { "Authorization": `Bearer ${currentUser.token}` }, body: formData });
                const data = await res.json();
                alert(data.message);
                fetchUserList();
            }

            async function kickout(username) {
                if(!confirm(`确定要强行踢出用户 ${username} 吗？`)) return;
                const formData = new FormData();
                formData.append("username", username);
                const res = await fetch('/api/admin/kickout', { method: "POST", headers: { "Authorization": `Bearer ${currentUser.token}` }, body: formData });
                const data = await res.json();
                alert(data.message);
                fetchOnlineUsers();
            }

            async function submitBatchTasks() {
                const files = document.getElementById("imageFiles").files;
                if (!files || files.length === 0) return alert("请至少选择一张图片！");

                const btn = document.getElementById("submitBtn");
                btn.innerText = "⏳ 批量上传与处理中...";
                btn.disabled = true;

                hasAutoDownloadedZip = false; // 重置自动打包标志

                const formData = new FormData();
                for(let i = 0; i < files.length; i++) formData.append("files", files[i]);
                formData.append("junk_size_mb", document.getElementById("junkSize").value);

                try {
                    const res = await fetch('/api/tasks/create_batch', {
                        method: "POST", headers: { "Authorization": `Bearer ${currentUser.token}` }, body: formData
                    });
                    if (res.ok) { 
                        document.getElementById("imageFiles").value = "";
                        fetchTasks(); 
                    } else { alert("提交失败"); }
                } catch(e) {
                    alert("网络异常，提交失败");
                } finally {
                    btn.innerText = "开始批量处理";
                    btn.disabled = false;
                }
            }

            async function fetchTasks() {
                if (!currentUser) return;
                const res = await fetch('/api/tasks', { headers: { "Authorization": `Bearer ${currentUser.token}` } });
                if (!res.ok) { if(res.status === 401) logout(); return; }
                const tasks = await res.json();
                const tbody = document.getElementById("taskTableBody");
                tbody.innerHTML = "";
                
                let completedCount = tasks.filter(t => t.status === "已完成").length;

                // 批次全完成后，触发极速 ZIP 自动下载
                if (tasks.length > 0 && completedCount === tasks.length && !hasAutoDownloadedZip) {
                    hasAutoDownloadedZip = true;
                    downloadZip();
                }

                tasks.forEach(t => {
                    const isRunning = t.status === "排队中" || t.status === "处理中";
                    let btn = "";
                    
                    if (isRunning) {
                        btn += `<button class="btn btn-danger" onclick="stopTask('${t.task_id}')">停止</button> `;
                    }
                    if (t.status === "已完成") {
                        btn += `<button class="btn btn-success" onclick="downloadFile('${t.task_id}')">单文件下载</button>`;
                    }
                    tbody.innerHTML += `<tr><td>${t.task_id}</td><td><b>${t.username}</b></td><td>${t.filename}</td><td>${t.status}</td><td>${t.created_at}</td><td>${btn}</td></tr>`;
                });
            }

            async function stopTask(id) {
                await fetch(`/api/tasks/${id}/stop`, { method: "POST", headers: { "Authorization": `Bearer ${currentUser.token}` } });
                fetchTasks();
            }

            async function clearTasks() {
                if(!confirm("确定要刷新并清除你的所有任务记录吗？")) return;
                const res = await fetch('/api/tasks/clear', { method: "POST", headers: { "Authorization": `Bearer ${currentUser.token}` } });
                if(res.ok) {
                    hasAutoDownloadedZip = false;
                    fetchTasks();
                }
            }

            function downloadFile(id) {
                const token = localStorage.getItem("token");
                if(!token) return alert("身份校验已失效，请重新登录");
                window.open(`/api/tasks/${id}/download?token=${token}`, '_blank');
            }

            // 【极速下载引擎：搭配后端 STORED 零压缩算法】
            function downloadZip() {
                const token = localStorage.getItem("token");
                if(!token) return alert("身份校验已失效，请重新登录");

                const zipBtn = document.getElementById("zipBtn");
                zipBtn.disabled = true;
                zipBtn.innerText = "⚡ 正在极速生成压缩包...";

                const xhr = new XMLHttpRequest();
                xhr.open("GET", `/api/tasks/download_zip?token=${token}`, true);
                xhr.setRequestHeader("Authorization", `Bearer ${token}`);
                xhr.responseType = "blob";

                xhr.onprogress = function(e) {
                    if (e.lengthComputable) {
                        const percent = Math.round((e.loaded / e.total) * 100);
                        zipBtn.innerText = `⚡ 极速打包下载中 ${percent}%...`;
                    } else {
                        zipBtn.innerText = `⚡ 极速传输数据中...`;
                    }
                };

                xhr.onload = function() {
                    if (this.status === 200) {
                        const blob = this.response;
                        const url = window.URL.createObjectURL(blob);
                        const a = document.createElement("a");
                        a.style.display = "none";
                        a.href = url;
                        a.download = `批量图片包_${new Date().getTime()}.zip`;
                        document.body.appendChild(a);
                        a.click();

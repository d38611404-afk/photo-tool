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

app = FastAPI(title="商丘情趣王一手科技 - 在线图片处理器")

# ---------------- 1. 模拟用户与任务数据库 ----------------
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
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效或已过期的 Token")
    return USERS_DB[TOKENS_DB[token]]

# ---------------- 2. 核心图像处理逻辑 ----------------
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
        raise HTTPException(status_code=403, detail="无权限停止他人任务")
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

# ---------------- 4. 嵌入网页界面 ----------------
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <title>商丘情趣王一手科技 - 在线处理控制台</title>
        <style>
            body { font-family: sans-serif; margin: 30px; background: #f4f4f9; }
            .card { background: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
            .hidden { display: none; }
            table { width: 100%; border-collapse: collapse; margin-top: 10px; }
            th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
            th { background-color: #f2f2f2; }
            .btn-danger { background-color: #f44336; color: white; border: none; padding: 5px 10px; cursor: pointer; border-radius: 3px; }
            .btn-success { background-color: #4CAF50; color: white; border: none; padding: 5px 10px; cursor: pointer; border-radius: 3px; }
            button { cursor: pointer; padding: 6px 12px; }
        </style>
    </head>
    <body>
        <div id="loginCard" class="card">
            <h2>商丘情趣王一手科技 - 系统登录</h2>
            <p style="color:#666;">
                <b>管理员账号：</b>admin / adminpassword<br>
                <b>普通用户账号：</b>user1 / 123
            </p >
            <input type="text" id="loginUsername" placeholder="用户名">
            <input type="password" id="loginPassword" placeholder="密码">
            <button onclick="login()">登录</button>
        </div>

        <div id="mainCard" class="card hidden">
            <h2>控制台 - 当前用户: <span id="userInfo"></span></h2>
            <button onclick="logout()">退出登录</button><hr>
            
            <h3>提交 JPEG 图片</h3>
            <input type="file" id="imageFile" accept=".jpg,.jpeg,.jpe,.jfif">
            垃圾数据大小 (MB): <input type="number" id="junkSize" value="1.0" step="0.5" style="width:60px;">
            <button onclick="submitTask()">开始处理</button>

            <h3>任务管理中心 <button onclick="fetchTasks()">手动刷新</button></h3>
            <table>
                <thead>
                    <tr><th>任务 ID</th><th>用户</th><th>文件名</th><th>状态</th><th>创建时间</th><th>操作</th></tr>
                </thead>
                <tbody id="taskTableBody"></tbody>
            </table>
        </div>

        <script>
            let currentUser = null, pollInterval = null;
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
                localStorage.clear(); currentUser = null; clearInterval(pollInterval);
                document.getElementById("loginCard").classList.remove("hidden");
                document.getElementById("mainCard").classList.add("hidden");
            }
            function showMainUI() {
                document.getElementById("loginCard").classList.add("hidden");
                document.getElementById("mainCard").classList.remove("hidden");
                document.getElementById("userInfo").innerText = `${currentUser.username} (${currentUser.role === 'admin' ? '总管理员' : '普通用户'})`;
                fetchTasks();
                clearInterval(pollInterval);
                pollInterval = setInterval(fetchTasks, 2000);
            }
            async function submitTask() {
                const file = document.getElementById("imageFile").files[0];
                if (!file) return alert("请选择文件！");
                const formData = new FormData();
                formData.append("file", file);
                formData.append("junk_size_mb", document.getElementById("junkSize").value);
                const res = await fetch('/api/tasks/create', { method: "POST", headers: { "Authorization": `Bearer ${currentUser.token}` }, body: formData });
                if (res.ok) { fetchTasks(); } else { alert("提交失败"); }
            }
            async function fetchTasks() {
                if (!currentUser) return;
                const res = await fetch('/api/tasks', { headers: { "Authorization": `Bearer ${currentUser.token}` } });
                if (!res.ok) return;
                const tasks = await res.json();
                const tbody = document.getElementById("taskTableBody");
                tbody.innerHTML = "";
                tasks.forEach(t => {
                    const isOwner = t.username === currentUser.username, isAdmin = currentUser.role === "admin";
                    const isRunning = t.status === "排队中" || t.status === "处理中";
                    let btn = "";
                    if (isRunning && (isAdmin || isOwner)) {
                        btn += `<button class="btn-danger" onclick="stopTask('${t.task_id}')">${isAdmin && !isOwner ? '强行停止(管理员)' : '停止'}</button> `;
                    }
                    if (t.status === "已完成") {
                        btn += `<button class="btn-success" onclick="downloadFile('${t.task_id}')">下载 .file</button>`;
                    }
                    tbody.innerHTML += `<tr><td>${t.task_id}</td><td><b>${t.username}</b></td><td>${t.filename}</td><td>${t.status}</td><td>${t.created_at}</td><td>${btn}</td></tr>`;
                });
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

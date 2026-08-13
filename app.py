import os
import time
import uuid
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from fastapi import FastAPI, File, UploadFile, Form, Depends, HTTPException, status, Header
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from PIL import Image

app = FastAPI(title="在线图片处理系统 - 极速流式版")

# 线程池并发处理，提高 CPU 利用率（设置 8 线程）
executor = ThreadPoolExecutor(max_workers=8)

# ---------------- 1. 用户与任务数据库 ----------------
USERS_DB = {
    "admin": {"username": "admin", "password": "adminpassword", "role": "admin", "bound_device": None},
    "user1": {"username": "user1", "password": "123", "role": "user", "bound_device": None},
    "user2": {"username": "user2", "password": "123", "role": "user", "bound_device": None}
}

TOKENS_DB: Dict[str, str] = {}
TASKS_DB: Dict[str, dict] = {}

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
            try: os.remove(input_path)
            except: pass
        if os.path.exists(temp_img_path):
            try: os.remove(temp_img_path)
            except: pass

# ---------------- 3. API 路由 ----------------

@app.post("/api/login")
async def login(
    username: str = Form(...),
    password: str = Form(...),
    device_id: str = Form(...)
):
    user = USERS_DB.get(username)
    if not user or user["password"] != password:
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
        
        # 提交到并发线程池
        executor.submit(background_process_image, task_id, input_path, output_path, junk_size_mb)
        task_ids.append(task_id)
        
    return {"message": f"已成功提交 {len(task_ids)} 个文件处理", "task_ids": task_ids}

@app.get("/api/tasks")
async def list_tasks(current_user: dict = Depends(get_user_from_request)):
    return [t for t in TASKS_DB.values() if t["username"] == current_user["username"]]

# 【🚀 终极优化：内存流式打包，秒级响应，无需读写本地磁盘 ZIP】
@app.get("/api/tasks/download_zip")
async def download_zip(current_user: dict = Depends(get_user_from_request)):
    user_tasks = [t for t in TASKS_DB.values() if t["username"] == current_user["username"] and t["status"] == "已完成"]
    if not user_tasks:
        raise HTTPException(status_code=400, detail="当前没有已完成的可供下载的文件")
        
    zip_io = io.BytesIO()
    with zipfile.ZipFile(zip_io, 'w', zipfile.ZIP_STORED) as zipf:
        for t in user_tasks:
            if t["result_path"] and os.path.exists(t["result_path"]):
                zipf.write(t["result_path"], arcname=t["out_name"])
                
    zip_io.seek(0)
    filename = f"processed_pack_{int(time.time())}.zip"
    
    return StreamingResponse(
        zip_io, 
        media_type="application/zip", 
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

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

# ---------------- 4. 前端页面 ----------------
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <title>在线图片处理系统 - 极速流式版</title>
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

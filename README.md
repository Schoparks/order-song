# order-song

在线点歌/听歌 Web 平台，支持多人房间、实时同步播放、歌单管理、B站/网易云搜索。

## 目录结构

- `backend/` — FastAPI + SQLite + WebSocket 后端
- `frontend/` — 纯 HTML/JS/CSS 静态前端（由后端一并托管）

## 功能概览

- 用户注册/登录，房间创建/加入/退出
- 搜索点歌（B站视频、网易云音乐）
- 播放列表管理（顶歌、删除、打乱、下一首）
- 多歌单管理（创建、重命名、删除、歌曲在歌单间移动）
- 导入网易云歌单（自动排除 VIP 专属歌曲）
- 热门歌曲排行
- 仅点歌 / 可放歌 模式切换
- 实时 WebSocket 同步（播放状态、队列变更）
- 管理后台（用户管理、房间管理、成员管理）

## 本地启动

### 1. 安装依赖

```bash
conda create -n order-song python=3.11 -y
conda activate order-song
pip install -r backend/requirements.txt
```

### 2. 启动服务

**推荐方式**（IPv4 + IPv6 双栈）：

```bash
python backend/run.py
```

默认监听端口 `5732`，可通过 `--port=` 自定义：

```bash
python backend/run.py --port=5732
```

### 3. 访问

- 前端页面：`http://localhost:5732/`
- 健康检查：`http://localhost:5732/health`
- 局域网访问：`http://<本机IP>:5732/`

## 管理后台

首次设置管理员需手动更新数据库：

```bash
sqlite3 backend/order_song.sqlite3 "UPDATE users SET is_admin = 1 WHERE username = '你的用户名';"
```

或使用：

```bash
python -c "import sqlite3; c=sqlite3.connect(r'backend/order_song.sqlite3'); c.execute('UPDATE users SET is_admin=1 WHERE username=?', ('你的用户名',)); c.commit()"
```

之后在登录页输入账号密码，点击右侧「管理端」按钮即可进入管理后台。管理员可以：

- 查看/删除用户、设置/取消管理员权限
- 查看/删除房间、移除房间中的特定成员


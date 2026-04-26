# order-song

在线点歌/听歌 Web 平台（前后端分离）。

## 目录

- `backend/`：FastAPI + SQLite + WebSocket
- `frontend/`：纯 HTML/JS/CSS 静态站点

## 本地启动（后端）

在 conda 环境内安装依赖并启动：

```bash
conda create -n order-song python=3.11 -y
conda activate order-song
pip install -r backend/requirements.txt
python -m uvicorn app.main:app --reload --host :: --port 5732 --app-dir backend
```

打开 `http://localhost:`5732`/health` 验证后端运行。

如果需要在同一局域网其他设备访问（例如 `192.168.x.x:`5732 或 `[fe80::x]:`5732），请确保：

- 使用 `--host ::` 同时监听 IPv4 和 IPv6 所有网卡
- Windows 防火墙允许入站访问端口 5732

## 本地访问（前端）

后端会托管 `frontend/` 静态文件，直接打开：

- `http://localhost:`5732`/`


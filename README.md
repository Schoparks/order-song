# order-song

在线点歌/听歌 Web 平台。

## 目录

- `backend/`：FastAPI + SQLite + WebSocket
- `frontend/`：纯 HTML/JS/CSS 静态站点

## 本地启动（后端）

在 conda 环境内安装依赖并启动：

```bash
conda create -n order-song python=3.11 -y
conda activate order-song
pip install -r backend/requirements.txt
python -m uvicorn app.main:app --reload --host "" --port 5732 --app-dir backend
```

打开 `http://localhost:5732/health` 验证后端运行。



## 本地访问（前端）



- `http://localhost:5732/`


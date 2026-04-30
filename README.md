# order-song

## 项目介绍

`order-song` 是一个多人在线点歌与房间同步播放平台。用户可以在浏览器中注册登录、创建或加入房间、搜索歌曲、加入播放队列，并在多台设备之间同步播放状态。

项目由 FastAPI 后端和 React 前端组成：

- 后端：`backend/`，提供用户认证、房间、队列、播放同步、歌单、搜索和管理相关 API。
- 前端：`frontend/`，基于 React + TypeScript + Vite 构建浏览器界面。
- 数据库：默认使用 SQLite，适合本地部署和轻量使用。

主要功能包括：

- 多用户房间与播放队列管理。
- Bilibili 和网易云音乐搜索。
- 网易云歌单导入。
- WebSocket 播放状态同步。
- 歌单收藏、热门歌曲和播放历史。
- 管理端用户与房间查看。

## 环境部署

### 后端环境

建议使用 Python 3.10 或更高版本。

创建并启用虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

安装后端依赖：

```powershell
python -m pip install -r backend/requirements.txt
```

复制配置模板：

```powershell
Copy-Item config_template.toml config.toml
```

首次部署时建议检查并修改 `config.toml` 中的配置，尤其是 `auth.jwt_secret`。

### 前端环境

建议使用 Node.js 18 或更高版本。

安装前端依赖并构建：

```powershell
cd frontend
npm install
npm run build
cd ..
```

构建完成后会生成 `frontend/dist/`，后端启动后会自动托管该目录中的前端页面。

## 启动方法

在项目根目录启动后端服务：

```powershell
python backend/run.py
```

默认访问地址：

- 应用首页：`http://localhost:5732/`
- 健康检查：`http://localhost:5732/health`

如需指定端口：

```powershell
python backend/run.py --port=5732
```

开发前端时，也可以单独启动 Vite 开发服务器：

```powershell
cd frontend
npm run dev
```

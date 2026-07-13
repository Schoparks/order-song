# order-song

## 项目介绍

`order-song` 是一个多人在线点歌与房间同步播放平台。用户可以在浏览器中注册登录、创建或加入房间、搜索歌曲、加入播放队列，并在多台设备之间同步播放状态。

项目由 FastAPI 后端和 React 前端组成：

- 后端：`backend/`，提供用户认证、房间、队列、播放同步、歌单、搜索和管理相关 API。
- 前端：`frontend/`，基于 React + TypeScript + Vite 构建浏览器界面。
- 数据库：默认使用 SQLite，适合本地部署和轻量使用。

主要功能包括：

- 多用户房间与播放队列管理。
- Bilibili、网易云音乐搜索，以及可选的 QQ 音乐搜索。
- 自动识别网易云/QQ 音乐链接的歌单导入；会员、失效和无完整播放地址的歌曲会被跳过。
- WebSocket 播放状态同步。
- 歌单收藏、热门歌曲和播放历史。
- 管理端用户与房间查看。

## 环境部署

### 后端环境

建议使用 Python 3.10 或更高版本，并使用项目约定的 Conda 环境 `order-song`。

创建并启用环境：

```powershell
conda create -n order-song python=3.10
conda activate order-song
```

安装后端依赖：

```powershell
python -m pip install -r backend/requirements.txt
```

复制配置模板：

```powershell
Copy-Item config_template.toml config.toml
```

启动时会把 `config_template.toml` 中新增而本地缺失的普通配置键原子补入旧 `config.toml`，已有值、注释、密钥和文件权限不会被覆盖；重复启动不会重复写入。如果旧文件使用的 inline/dotted table 与新模板结构冲突，合并会安全跳过并由程序默认值兜底，不会写坏原配置。

QQ 音乐默认关闭。需要时把 `config.toml` 中的 `qq_music.enabled` 改为 `true`；首次启动会在服务端控制台尝试渲染登录二维码，同时原样保存 QQ 上游 PNG 并打印绝对路径；如终端字体或行距影响扫码，请直接打开 PNG。PNG 会在扫码成功、二维码过期或服务停止后删除。登录态会保存到本机并自动续期。该功能直接使用 `backend/requirements.txt` 中的 Python 支持库，不需要另外部署服务或辅助进程。QQ 音乐凭证、设备和登录二维码文件已加入 `.gitignore`，不要上传或共享。

### QQ 音乐多 IP 与账号风险

QQ Cookie 和登录态只保存在服务端，不会下发给浏览器；项目下发的是临时 `purl`/`vkey` 直链，客户端会从各自的公网 IP 直接访问 QQ CDN。因此在多人使用时，可能出现多个 IP 使用同一服务端账号签发的临时凭证。QQ 音乐未针对这种非官方接入公开稳定的 IP 绑定或并发阈值保证，实际使用中可能遇到 `403`、限流、直链提前失效或账号风控，但不代表一定会封号。当前仅保留免费且可完整播放的歌曲，可降低会员权益共享问题，但不能消除反滥用风险。临时直链等同短期访问凭证，不应写入日志、复制或转发。

建议仅在可信、小规模的局域网或私有环境中启用。公网多人部署时，优先让多数客户端使用“仅点歌”，或指定单一播放端；无法接受相关风险时，请保持 `qq_music.enabled = false`。项目不会通过后端代理 QQ 音频；若未来存在可用的官方授权方案，更彻底的账号隔离方向是由每位用户独立授权。

首次启动还会把 `config.toml` 中精确匹配模板的 `auth.jwt_secret` 占位值原子替换为持久随机密钥；如果配置文件不可写，应用会拒绝使用占位密钥启动。正式部署仍推荐通过 `ORDER_SONG_JWT_SECRET` 环境变量注入至少 32 字节的随机密钥。

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

## 验证

后端语法和回归测试：

```powershell
conda activate order-song
python -m compileall -q backend
python -m unittest discover -s backend/tests
```

前端类型检查与生产构建：

```powershell
cd frontend
npm run build
```

开发前端时，也可以单独启动 Vite 开发服务器：

```powershell
cd frontend
npm run dev
```

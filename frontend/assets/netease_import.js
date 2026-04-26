import { api } from './api.js';
import { loadPlaylists } from './playlist.js';

export function showNeteaseImportModal() {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";

  const modal = document.createElement("div");
  modal.className = "modal";
  modal.innerHTML = `
    <div class="sectionTitle">导入网易云歌单</div>
    <div class="muted" style="margin-bottom:10px">请输入网易云歌单链接或ID，VIP及不可用歌曲将被自动排除</div>
    <div class="row">
      <input class="input js-url" placeholder="歌单链接或ID" />
    </div>
    <div class="row" style="margin-top:8px">
      <input class="input js-name" placeholder="歌单名称（可选，默认使用原名）" />
    </div>
    <div class="js-status hint"></div>
    <div class="actions">
      <button class="btn small js-cancel">取消</button>
      <button class="btn small js-import">导入</button>
    </div>
  `;

  const statusEl = modal.querySelector(".js-status");
  const importBtn = modal.querySelector(".js-import");
  const cancelBtn = modal.querySelector(".js-cancel");
  const urlInput = modal.querySelector(".js-url");
  const nameInput = modal.querySelector(".js-name");

  let importDone = false;

  importBtn.addEventListener("click", async () => {
    if (importDone) {
      overlay.remove();
      loadPlaylists();
      return;
    }
    const url = urlInput.value.trim();
    if (!url) {
      statusEl.textContent = "请输入歌单链接或ID";
      return;
    }
    importBtn.disabled = true;
    importBtn.textContent = "导入中…";
    statusEl.textContent = "正在获取歌单信息…";
    try {
      const result = await api("/api/playlists/import-netease", {
        method: "POST",
        json: { url, name: nameInput.value.trim() || "" },
      });
      statusEl.textContent = `导入成功！共${result.total}首，已添加${result.added}首${result.skipped > 0 ? `，跳过不可用歌曲${result.skipped}首` : ""}`;
      importDone = true;
      importBtn.textContent = "完成";
      importBtn.disabled = false;
      cancelBtn.textContent = "关闭";
    } catch (e) {
      let msg = "导入失败";
      try { msg = JSON.parse(e.message).detail || msg; } catch (_) { msg = e.message || msg; }
      statusEl.textContent = msg;
      importBtn.disabled = false;
      importBtn.textContent = "重试";
    }
  });

  cancelBtn.addEventListener("click", () => overlay.remove());
  overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });

  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  urlInput.focus();
}

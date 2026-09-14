# ArchBro 固定參考專案

後續架構生成、關聯呈現與 Canvas 優化，固定使用這三個專案比較。這些是長期保留的假想業務專案，**不是跑完就刪的 acceptance fixture**。不要重新生成一批不同內容來比較 UI，也不要把測試清理套用到這三個 project ID。

| 固定樣本 | 節點／最小責任單位 | 已存關聯／跨系統關聯 | 固定流程 |
| --- | --- | --- | --- |
| 01 電商訂單到交付 | 36／24 | 42／17 | 結帳付款；出貨通知；付款遙測 |
| 02 協作與 AI 工作平台 | 40／31 | 63／29 | AI 排程；文件同步；人工審查稽核 |
| 03 智慧製造與供應鏈 | 40／34 | 76／36 | 排程與現場執行；品質與交付；數位分身回饋 |

每條箭頭由動作發起者指向接收者，標籤為「動作 · 通訊方式」。框的包含關係表示責任分解，不代表呼叫關係。所有最小責任單位在無向連通性檢查下屬於同一個專案；九條指定的有向業務流程都有實際邊支援。這項連通檢查不能取代人工判斷業務關係是否合理。

目前已部署的 Architecture 契約限制總節點 40、深度 3；規劃紀錄的 80 筆上限並不等於可儲存 80 個架構節點。v1 在這個真實限制內，逐步增加跨域互動與回饋迴路。製造樣本以子系統為最小責任單位，同一責任下的 UI、引擎與設備細節明寫在 responsibility，沒有冒充成獨立節點。

## 唯一固定輸入

`v1/01-commerce.json`、`v1/02-ai-workspace.json`、`v1/03-manufacturing.json` 各自保存：

- 原始需求 `source_requirement`、穩定元件 ID、完整責任與階層。
- 可直接交給 `archbro_bootstrap_project` 的 `payload`，包含關聯、三個初始任務及完整 planning trace。
- 三條固定流程的起終點、每一步、觀察焦點及驗收描述。

`v1/corpus.lock.json` 固定完整定義、WebMCP payload、正規化 Architecture 的 SHA-256。Hash 算法是 UTF-8 JSON、鍵排序、不轉義非 ASCII 字元、無額外空白。v1 已發布；不要覆寫 lock 讓漂移看似通過。需要改需求或責任邊界時建立明確的新 corpus 版本，並保留 v1 比較基準。

## 固定生產與檢查流程

在 `C:\AI\archbro` 執行。使用既有 Python 專案依賴；live schema 驗證需要 `jsonschema`，視覺記錄另需 `playwright`。沿用已登入的 Zenu Workbench 原生頁面，不啟動新瀏覽器、不匯出登入狀態、不帶出 token。

1. **驗證相同輸入**：用真正後端 model 檢查 40 節點契約、階層、planning trace、任務歸屬；再檢查 leaf endpoints、業務動作標籤、無孤島與指定路徑。
2. **只建立缺少的樣本**：透過既有 Workbench WebMCP 建立 Architecture v1；每次回應立刻保存 project ID 與建立紀錄。這個固定發布流程不呼叫付費 provider。
3. **讀回比對**：三份 Architecture 與 lock 完全一致、三個 TODO 任務不漂移、沒有待審提案；檢查 root projection 與每個樣本的三條 live 路徑及 node context。
4. **固定視角比較**：每個專案拍「完整 Fit 總覽」及「第一情境焦點、100% 再放大一次至 125%」。保存 CSS viewport、DPR、viewBox、縮放級別、實際顯示邊數與 console/network 事件。拍攝後重新證明 accepted Architecture 未變。

```powershell
python -X utf8 qa/reference_projects.py validate

# 已建立的正式樣本，後續預設只驗證。
python -X utf8 qa/reference_projects.py verify --evidence C:\AI\temp\archbro\reference-comparison-RUN_ID

# 首次建立或補齊 registry 明確缺少的樣本時才用 publish；有 ID 就讀回驗證。
python -X utf8 qa/reference_projects.py publish --evidence C:\AI\temp\archbro\reference-comparison-RUN_ID

# 使用同一既有 Workbench 原生頁面的 CDP；不啟動新 profile。
python -X utf8 qa/reference_project_views.py --evidence C:\AI\temp\archbro\reference-comparison-RUN_ID\views
```

`RUN_ID` 必須替換成每次比較的新識別，避免覆寫歷史。Registry 固定在 `.demo/reference-projects/registry.json`，不能為了重跑任意改成新 registry。程式會拒絕尚未確認結果的 `PUBLISHING` 紀錄、payload 漂移及已存 Architecture 漂移，不會自動改寫架構或自動重送未知結果的建立請求。若本機 registry 遺失，先用已保存 bootstrap evidence 和下方 project IDs 恢復並讀回核對，不要再建三個同名專案。程序異常留下的 `.lock` 也必須先確認沒有仍執行中的發布程序再處理。

## 比較方式

**Canvas／UI 比較**使用同一組 v1 資料與 project IDs、任務 TODO 狀態、沒有選取或折疊的初始狀態、相同視窗大小及上述視角。記錄實際 runtime identity 與截图時間；viewport 不同就不能聲稱是同条件畫面比較。分別記錄可讀性、關聯辨識、文字或線段重疊、路徑與 Inspector、縮放以及資料不變性。API 資料 PASS 不等於視覺可讀性 PASS。

**生成流程比較**固定取三份 `source_requirement` 當需求，另存 producer/model/version/config、輸入 hash、每個規劃步驟、原始輸出及用量（未呼叫模型就是 NOT_RUN，不能填 0 假冒 observed usage）。逐份比對 root 職責、完整分解、具動作的跨域連線、九條業務流程、leaf 合理性與 schema。生成內容可以不同，但不得替換這三個已接受的視覺基準。需要實際 provider 試驗時另外記錄付費呼叫結果；本次人工編寫的固定 corpus 不是 provider 品質驗收。

## 已發布專案與第一版觀察

正式 runtime：`bb15dd8054b80573c861699c544b1cf90a6dff44`；本次只新增專案資料與比較工具，沒有重建或替換已部署 image。

- [01 電商完整 Canvas](https://archbro-jim.magicdala.com/?canvas=architecture&project=project_4f01fc557d3b4dcb86557d63456ea2ab&reference=v1) · [結帳焦點](https://archbro-jim.magicdala.com/?canvas=architecture&project=project_4f01fc557d3b4dcb86557d63456ea2ab&node=checkout_api&reference=v1)
- [02 AI 工作平台完整 Canvas](https://archbro-jim.magicdala.com/?canvas=architecture&project=project_2bb9d319c1d447f7acdf1df0ec7f075c&reference=v1) · [代理執行焦點](https://archbro-jim.magicdala.com/?canvas=architecture&project=project_2bb9d319c1d447f7acdf1df0ec7f075c&node=agent_worker&reference=v1)
- [03 製造供應鏈完整 Canvas](https://archbro-jim.magicdala.com/?canvas=architecture&project=project_2a8ed3ed93fa4c5b94526d99e2952f87&reference=v1) · [製造執行焦點](https://archbro-jim.magicdala.com/?canvas=architecture&project=project_2a8ed3ed93fa4c5b94526d99e2952f87&node=mes_dispatch&reference=v1)

首次 evidence：`C:\AI\temp\archbro\reference-projects-20260906`。三份真實後端讀回 hash 相符，9/9 WebMCP 路徑 FOUND，所有觀看操作後 Architecture 仍是原來的 v1。原先「Core Platform／Operations」測試圖沒有跨 root 關係，是刻意測試 UNREACHABLE 的小型 fixture，不適合作為產品示範。

這組大型樣本也留下了具體的後續優化基準：

- 全景 Fit 下的文字與關聯標籤偏小，圖的縱橫比例讓部分畫面留下大片空白。
- 三個 Canvas 當前 MAP 投影分別只顯示 23／30／33 條關聯；後端實際已存的是 42／63／76。放大到 `zoom_tier=full` 時 `reading_mode` 仍為 `MAP`，顯示邊數沒有恢復。這是呈現與閱讀模式的落差，不是樣本缺少關係。
- UI 將 MAP 子集標成 `canonical relationships`，容易讓人誤以為那些就是所有關係。後續應明確區分已存、目前呈現及聚合數量。
- 原始觀察保留 Cloudflare beacon 的 CSP 拒絕及導覽時的 request-aborted 事件；不要把截圖完成說成 console/network 全無錯誤。

這些現象是此版本的**已記錄問題**，沒有以「三個樣本建立成功」冒充畫面已完全優化。後續修復要保持這些固定輸入，再用同樣視角證明差異。

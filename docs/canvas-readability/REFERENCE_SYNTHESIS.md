# ArchBro 參考研究與規則修訂

2026-09-06。範圍：設計研究與規則，不改 ArchBro 程式，不安裝或執行參考專案。

已讀完整的使用者補充清單。本輪核查其中 11 個優先項目的官方文件／README，並沿用先前已讀的 Archify 設計文件。其他 28 個項目保留在參考庫，尚未逐一深讀。Stars、授權及維護快照不作本次設計結論的依據；沒有安裝實測，也不把文件描述當成精度、效能或產品適用性的實驗證據。

來源清單：[使用者貼入的完整研究](C:/Users/JT_Home/.codex/attachments/441d6d8f-1967-450c-adc7-16625c63a533/pasted-text.txt)。其中另一個環境的 sandbox 下載連結未作本地已存在檔案使用。

以下「參考事實」來自官方文件；「ArchBro 規則」是本次設計判斷，仍是草案。

| 參考與本輪讀取範圍 | 文件中可確認的概念 | 對 ArchBro 的設計判斷 |
| --- | --- | --- |
| [LikeC4 Views](https://likec4.dev/dsl/views/) | 視圖是模型在不同 scope／detail 下的投影，可命名、延伸及引用。 | 閱讀視圖具穩定 ID、模型版本與明確集合。保留全階層預設，不採其未定義 index 時只納入 top-level 的預設。 |
| [Structurizr DSL](https://docs.structurizr.com/dsl/language#dynamic-view) | 靜態模型與 views 分開；dynamic view 表達模型關係的具體互動與順序。 | 專案主幹和具名流程各有用途。具名流程的步驟引用真實關係；不可把靜態依賴路徑直接宣稱為 runtime 執行序列。 |
| [Archify Design](https://github.com/tt-a1i/archify/blob/main/DESIGN.md)，前輪已讀 | 主路徑優先、按需揭露、一處承接細節，語意色彩與有限互動。 | 首頁建立全貌，選取後才讀細節。保留自己的品牌、中文排版、接受架構與 Human Review 機制。 |
| [D2 Layouts](https://d2lang.com/tour/layouts/) | 可選用不同 layout engines；支援選項與輸出效果依引擎而異。 | 模型、布局與渲染分工；引擎是可評估的實作選擇，不能代替架構模型。不能只因能出圖就判定適用。 |
| [ELK Layered](https://eclipse.dev/elk/reference/algorithms/org-eclipse-elk-layered.html) | Layered layout 支援 ports、多重邊、正交路由及啟用相應選項的跨階層 compound graphs。 | 把 containment、端口、跨域走線一起設計。列作 Backend 布局候選；尚未選型或證明 deterministic。 |
| [React Flow Layouting](https://reactflow.dev/learn/layouting/layouting) | 畫布元件與第三方布局／路由分工；文件另列子圖跨界布局的限制。 | 接收後端幾何的 renderer 不構成第二套架構真相。禁止的是未授權改 canonical 模型或前端自行重排；目前不做 library migration。 |
| [Understand-Anything README](https://github.com/Egonex-AI/Understand-Anything) | 搜尋、節點解說、guided tours、domain view；結構解析與 LLM 語意整理有所區分。 | 用具名流程與解說引導理解；保留原圖位置。自動導覽只可解釋有來源的內容，不把推論領域直接變成 accepted containment。 |
| [Graphify README](https://github.com/Graphify-Labs/graphify) | 區分直接擷取與推導的關係，提供 explain／query 等讀取入口。 | 分開來源方法、接受狀態及證據有效性；有界查詢附上完整性資訊。EXTRACTED 不等於 runtime observed，也不等於架構已接受。 |
| [CodeBoarding README](https://github.com/CodeBoarding/CodeBoarding) | 以靜態分析和 LLM 整理高層架構、子系統說明及增量輸出。 | 程式碼符號到架構元件的 mapping 要有來源與不確定性。輸出先作 Evidence，不自動覆蓋 Living Architecture。 |
| [EventCatalog README](https://github.com/event-catalog/eventcatalog) | 文件連結 domains、services、messages、schemas；business flows 引用既有 services／messages。 | 邊可連到消息／API 契約／文件與版本。圖上只留短動作，Inspector 和 Agent 才取所需細節；缺資料照實為未知。 |
| [Backstage Relations](https://backstage.io/docs/features/software-catalog/well-known-relations/) | 方向與 relation type 明確；partOf、dependsOn、providesApi、ownedBy 有不同意思，owner metadata 不用來授予 runtime 權限。 | 歸屬、呼叫、消息、部署及負責人分開建模／呈現。Inverse query 是同一關係的反向讀法，不能當作另一條業務回呼。 |
| [Diagram Design README](https://github.com/cathrynlavery/diagram-design) | 有資訊簡化、fidelity ledger 與實際渲染檢查；部分輸入模式會合併／收縮／省略內容。 | 採用顯示差異記錄及實際畫面檢查。全展開 Canvas 不採刪元件／合併層級的簡化策略，不能為漂亮改固定樣本。 |

1. **從單張圖提升為有明確範圍的閱讀視圖**

   同一 Accepted Architecture 上，定義「專案主幹」「具名業務流程」「全部關係」。每個持久化視圖要能指出穩定 view ID、架構版本、所引用 node／edge IDs、用途及來源；使用者的鏡頭與選取是另外的暫時狀態。全展開 Canvas 內切換這些讀法，沿用同一份座標。

   主幹是結構摘要，不能冒充業務的唯一成功路徑；具名流程也不代表觀測到的 production trace。不能借多視圖之名，恢復只看 Root 再逐層點開的首頁。

2. **將布局要求寫成可比較的約束**

   硬條件：保留 canonical 身分與方向、合法 containment、同層無重疊、文字安全、線不穿卡片／標題、端點可辨識。滿足後再比較主幹交叉、共享通道、彎折、總線長、空白與全圖比例。

   不把左至右當成每條 edge 都必須遵從的語意。循環與回呼是真實關係，需要清楚的回流通道。布局版本需固定輸入正規化、引擎／選項、尺寸與字體度量政策。Frontend 的字體替代只可安全揭露／省略文字，不得改 canonical 幾何。

   ELK／D2 等可作參考或候選；採用與否需日後在相同樣本上比較。此輪沒有換引擎，也沒有聲稱某引擎保證最優。

3. **讓關係能回答工程問題**

   一條關係至少回答：誰對誰、做什麼、什麼方向、根據什麼。若已有契約資料，可進一步讀取 API／message／schema／版本與文件。不要將所有關係改名成 DEPENDS_ON，也不要為補欄位猜協定、endpoint 或 schema。

   「屬於」由容器表達；「依賴／呼叫／傳遞」由相應關係表達。catalog ownership 留在 Inspector，不能冒充架構 containment，更不能授予帳號操作權限。真實回呼要有自己獨立的 canonical 關係；查詢同一關係的 inverse 不生成回呼。

4. **人類與 Agent 對同一份事實有可對照的入口**

   人類先讀全貌、再選元件或流程。Agent 依問題取得有界的結構化內容，不必每次吞全部架構，也不應靠 SVG 或截圖取得工程事實。回應要表明 project、Architecture version、scope、IDs、查詢限制及是否截斷。看不到、查不到、未授權與確定不存在不能混在一起；權限不足時也不能洩漏被隱藏對象。

   全量授權資料可被查詢，不等於每次 provider 呼叫都應送全量。Context Tray、預覽、實際送出與 provider telemetry 仍依既有 exact manifest 契約；視圖隱藏不可以默默縮減 context。

   證據至少分三個問題：

   | 問題 | 意義 |
   | --- | --- |
   | 怎麼得知 | 人工聲明、程式解析、resolver 推導、LLM 推論或真正 runtime observation；記錄相應來源 |
   | 是否接受為架構 | 經 Recommendation／Human Review 的 PENDING、ACCEPTED、REJECTED 等既有流程；不適用者明示不適用 |
   | 此來源目前是否適用 | 綁定 revision／environment／時間；有效、過期或未知，不能用 confidence 分數掩蓋失效 |

   一個被人審接受的推論，來源仍是推論；一個 parser 直接找到的 import，也不會因此自動成為接受架構的一條 service call。

5. **驗收同時涵蓋幾何、理解和回答**

   用原三個樣本、原九條流程與固定畫面狀態保存比較。除了座標／交叉／端點，還要查看實際字體下的渲染、箭頭裁切、浮層遮擋、中文／長識別字及鍵盤選取。視圖需能交代哪些資訊被隱藏，以及如何取回。

   人類任務：指出責任域、說明一條流程、找到上下游、判斷某條線的動作與來源。Agent 任務：回答相同問題並引用一致 IDs／版本，對推論、過期與查詢限制照實標記。未來可記錄成功率、找尋步驟和時間；目前未執行使用者研究，不能宣稱已達最佳體驗。

   三個樣本只有 36–40 個架構節點，不能據此宣稱數百／數千節點的全展開可讀性或效能已被驗證。

本輪保留但未深讀的 28 個參考：Diagrams、Mermaid、PlantUML、C4-PlantUML、Kroki、Archi；Next AI Draw.io、Architecture Diagram Generator；GitNexus、DeepWiki Open、GitDiagram、dependency-cruiser、Madge、code2flow、Swark；Excalidraw、draw.io Desktop、tldraw、drawDB；G6、X6、Cytoscape.js、Dagre、Sigma.js；InfraMap、KubeDiagrams；CloudMapper、FossFLOW。

目前沒有 library 選型、整合、授權結論、實測或部署。研究成果只更新規則草案；不得將先前準備中的程式稿當作已符合本輪修訂。

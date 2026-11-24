# 微缩胶卷跨卷补拍采录台

面向馆藏数字化操作员、复核员和版本交付员。登记版式帧码，核对相邻卷的重叠图像元数据，采录实扫与质检回执，通过馆方签收处理漏页补拍，生成可验算的交付清单。样例使用合成的本地 JSON，实扫输入是设备导出后的回执，包含图片摘要、采集分钟和操作员会话。首版处理元数据和清单，图像文件、设备驱动、OCR 和远端交付均在运行范围之外。

## 快速运行

在仓库根目录使用 Python 3.11 或更新版本。SQLite 与 unittest 随 Python 安装，无额外依赖。

```powershell
python -B -m microfilm_reshoot --db demo.sqlite demo
python -B -m microfilm_reshoot --db demo.sqlite audit
python -B -m microfilm_reshoot --db demo.sqlite history NEWS-42
python -B -m microfilm_reshoot --db demo.sqlite manifest NEWS-42 --version 2
python -B -m unittest discover -s tests -v
```

`demo` 把 14 批回执实际写入指定 SQLite，重复运行相同夹具保留历史。建议用专用空库观察完整过程。`audit` 以只读方式打开文件库，按原始收件顺序重算所有业务表、历史报告和交付清单。`MATCH` 返回 0，差异返回 1，命令或输入错误返回 2。`manifest` 默认只取现行版，指定版本可查看已撤回的原始清单及状态。

## 手动录入

每条命令使用同一个 `--db`，以下示例为一个独立的新数据库。

```powershell
python -B -m microfilm_reshoot --db work.sqlite init
python -B -m microfilm_reshoot --db work.sqlite layout fixtures/demo/01_layout.json
python -B -m microfilm_reshoot --db work.sqlite catalog NEWS-42
python -B -m microfilm_reshoot --db work.sqlite seam fixtures/demo/02_seam.json
python -B -m microfilm_reshoot --db work.sqlite seam-check NEWS-42
python -B -m microfilm_reshoot --db work.sqlite capture fixtures/demo/03_capture.json
python -B -m microfilm_reshoot --db work.sqlite scans
python -B -m microfilm_reshoot --db work.sqlite tender fixtures/demo/04_tender.json
python -B -m microfilm_reshoot --db work.sqlite library fixtures/demo/08_library.json
python -B -m microfilm_reshoot --db work.sqlite show NEWS-42
```

输入为含 `id`、`kind`、`data` 的事件数组。每个导入命令接收该领域事件，`ingest FILE` 支持混合批。版次须先于卷登记、馆方漏页单和交付申请登记，也可在同一批中置于前面。签批、质检、见证和实扫引用可迟到。有待件或冲突的有效回执会入账。格式错、版式错或相同事件编号内容不同时整批回滚。对同一数据库有多个写入者时，SQLite 事务串行提交批次。

| 入口 | 实体和规则 | 反例夹具 |
| --- | --- | --- |
| `layout` / `catalog` | 版次声明卷数和页数，卷声明顺逆方向，帧码由卷号与四位物理槽号生成。物理顺序必须与声明同向，页码属于版次。 | `bad_layout.json` 的反向帧被标为正卷，批次拒绝。 |
| `seam` / `seam-check` | 相邻卷边界须共享至少两张连续页。两种角色各自取最近有效分钟的完整见证组，签署人须独立。签署的配对须涵盖全部共同页并按逻辑页序，页指纹须相同。认证后按共同指定的主卷裁定帧码。 | `bad_seam.json` 双方同时倒列完整帧对，仍不能认证。 |
| `capture` / `scans` | 实扫回执必须落在操作员会话的左闭右开窗口，帧码和内容指纹对应，评分至少 70。质检在实扫发生后生效，同刻冲突挂起。主帧原扫按最高分选图，同分异图须等待，等图优先保留最早回执。 | `bad_capture.json` 分别演示闭班时刻和指纹错配。 |
| `library` / `show` | 每页最新漏页单生效，同刻两单稳定挂起。签批可接受、重开、撤销漏页判断。批准扫描须带本单号并落于同一逻辑页，拍摄时间介于通知和签收之间。相邻认证卷的备用帧可用于有效补拍，交付同时记录主帧与实扫帧。 | 在演示末段后导入 `bad_library.json`，误签原扫使版本失效。 |
| `tender` / `manifest` | 声明卷、卷间边界和每页实扫都齐备才发版。清单变化或证据阻断撤回现行版；证据恢复需新申请。 | 封版前可导入 `tender_denied.json`，历史记录不产生版本的申请。 |

## 批次与版本

批次顺序决定何时知道事实，有效分钟决定同一类证据的先后。每个落库批次按最终事务状态保存版次报告和版本转移。一批补齐相互依赖的证据可避免记录一个实际未对外可见的中间状态。相同原件重试返回 `ALREADY_IMPORTED`，不同原件中的相同事件可重叠导入。已签发清单保存 SHA-256，并含帧码、实扫事件、图片摘要、会话人员和补拍单号。撤回只影响现行资格，历史清单仍可按编号核对。

`layout.py` 管版式，`seams.py` 裁定重叠，`capture.py` 检查实扫和质检，`library.py` 处理馆方单据，`delivery.py` 处理清单和版本，`ledger.py` 管原始收件、事务和重算。所有夹具、回执字段和合成标识均可在本地查看。

## Text strip preview

After the first three demo batches are present, `python -B -m microfilm_reshoot --db work.sqlite transcript fixtures/transcript.json` builds a plain-text preview from synthetic text-strip receipts. It checks the source scan, keeps scan and frame references, orders non-overlapping strips, and reports the output span for every original strip. It does not write to the filing ledger.

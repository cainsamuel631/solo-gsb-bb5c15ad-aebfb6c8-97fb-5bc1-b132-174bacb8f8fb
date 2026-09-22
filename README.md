# 字幕断句校验台（Subtitle Segmentation Checker）

上传带时间码的 **WebVTT / SRT** 字幕，设定**每行字数**、**最小停留时间**、**最大阅读速度**，
系统对跨 cue 的文本做**动态规划断句**，必要时在相邻 cue 间**重分配边界**，
并标出**无法满足约束的最小区段**及原因。支持按语言筛选问题、导出修订后的 WebVTT。

技术栈：**FastAPI + SQLite + 原生 HTML/CSS/JS**（无前端构建、无框架）。

---

## 一键启动

需要 [Docker](https://docs.docker.com/get-docker/) 与 Docker Compose（v2）：

```bash
docker compose up --build
```

然后浏览器访问：

> **http://localhost:8000**

停止：`Ctrl+C`；后台运行：`docker compose up --build -d`；清理数据卷：`docker compose down -v`。

数据持久化在 Docker 卷 `subtitle-data`（容器内 `/data/subtitles.db`），容器启动时自动建库建表。

### 不用 Docker（本地开发）

```bash
pip install -r requirements.txt
export SUB_DB_PATH=./data/subtitles.db
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

访问 http://localhost:8000 。

---

## 使用方法

1. 在页面顶部选择 `.vtt` / `.srt` 文件（支持拖拽）。
2. 设定阈值：

   | 参数 | 默认 | 含义 |
   |---|---|---|
   | 每行最大字数 | 中文 21 / 英文建议 42 | 单行字符上限；每个 cue 最多 2 行，超出必须拆分 |
   | 最小停留 (ms) | 1000 | cue 的最短展示时长 |
   | 最大阅读速度 (字/秒) | 17 | `字符数 / 时长` 的上限；达到 90% 给出黄色预警 |
   | 跨 cue 合并间隙 (ms) | 250 | 相邻 cue 间隙不超过该值时，允许 DP 跨界重分配文本 |

3. 点击「开始校验」，查看：
   - **统计总览**：原始 / 修订 cue 数、硬约束违规数、预警数，自动识别语言；
   - **问题清单**：可按**语言**与**类型**（超字数 / 停留过短 / 语速过快 / 不可满足）筛选；
   - **修订后断句**：逐条展示新时间码、文本折行、实际字/秒与行长；
4. 点击「导出修订 WebVTT」下载 `revised_<id>.vtt`。

`samples/` 目录提供了中文（SRT）与英文（VTT）两个内置了各类问题的示例文件。

---

## 工作原理

### 1. 解析（`app/parser.py`）

- 兼容 WebVTT（`WEBVTT` 头、`NOTE/STYLE/REGION` 块、cue 标识）与 SRT（数字序号、逗号时间码）；
- 去除 `<c>`、`<b>`、卡拉 OK 时间戳、`{\an8}` 等内联标签，保留多行文本；
- **分词**：拉丁语单词整体保留（不拆词），汉字 / 假名 / 谚文逐字成 token；`join_tokens` 可逆；
- 基于 Unicode 区段做语言识别（中 / 日 / 韩 / 拉丁 / 西里尔 / 阿拉伯）。

### 2. 动态规划断句（`app/analyzer.py`）

1. **分段**：相邻 cue 时间间隙 ≤ 合并阈值时，把它们的 token 流拼成一个可重组的段；
2. **重建时间轴**：在每个原始 cue 内按 token 宽度线性插值，得到每个 token 边界的时间位置，
   并强制单调不减（兼容重叠时间轴）；
3. **DP**：`dp[j] = min(dp[i] + cost(tokens[i:j], t[i], t[j]))`，
   切点代价包含三类硬约束惩罚（行长 50/超字、停留 0.2/不足毫秒、语速 8/超速单位），
   外加结构代价（新增 cue 固定开销、跨越原边界开销、句末标点行首惩罚）——
   **只有收益足够大时才会拆分或跨 cue 借字，平局时保持原边界**；
4. **最小区段标注**：DP 把文本切到最小代价粒度后仍违反硬约束的 cue，
   即为「无法满足约束的最小区段」，附带：
   - 违反的约束类型（行长 / 停留 / 语速）；
   - 实际值与阈值；
   - 覆盖的原始 cue 范围与区段原文；
   - `reason_summary`：说明在该时间轴下无论边界如何重分配都无法满足的原因。

文本 token 在重排前后**严格守恒**（见 `test_cross_cue_reflow_preserves_tokens`）。

### 3. 持久化（`app/database.py`）

三张表：`uploads`（原始文件）、`analyses`（参数、完整结果 JSON、修订 VTT）、
`issues`（逐条问题，带语言与类型索引，供跨任务筛选）。

### 4. API

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/analyze` | multipart 上传 + 阈值参数，返回摘要 |
| `GET` | `/api/analyses/{id}` | 任务详情（修订 cue 列表、问题、参数、统计） |
| `GET` | `/api/export/{id}.vtt` | 下载修订后的 WebVTT |
| `GET` | `/api/issues?language=&type=` | 按语言 / 类型筛选问题 |
| `GET` | `/api/languages` | 问题语言聚合（筛选用） |
| `GET` | `/api/analyses` | 最近任务 |
| `GET` | `/` | 前端页面 |

交互文档（Swagger）：http://localhost:8000/docs

### 调用示例

```bash
curl -F "file=@samples/sample_zh.srt" \
     -F "max_chars=21" -F "min_dwell_ms=1000" -F "cps_max=17" \
     http://localhost:8000/api/analyze

curl -OJ http://localhost:8000/api/export/1.vtt
```

---

## 测试

```bash
python3 -m unittest discover -s tests -v
```

核心算法（解析、分词、DP 断句、约束校验、WebVTT 回写）共 20+ 用例，仅依赖标准库；
API 端到端用例在装好 `requirements.txt` 后自动启用。

## 项目结构

```
app/
  main.py        FastAPI 路由与启动初始化
  parser.py      VTT/SRT 解析、时间码、分词、语言检测
  analyzer.py    分段 + 动态规划断句 + 约束校验 + VTT 渲染
  database.py    SQLite 建表与 CRUD
  static/        原生前端（index.html / style.css / app.js）
samples/         中/英文示例字幕
tests/           核心算法与 API 测试
Dockerfile
docker-compose.yml
docker-entrypoint.sh   启动时初始化数据库
```

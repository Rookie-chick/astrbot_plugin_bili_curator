# astrbot_plugin_bili_curator

> 让 QQ 里的 AI 会刷 B 站：按需搜索/刷榜、按分类收藏到你的账号、按需评论，
> 并且**它自己知道自己在刷**。

一个 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件。全部动作由用户口令触发，**没有定时任务**。

---

## 功能

| 你说 | 它做 |
|---|---|
| `B站登录` | 生成二维码，扫码后自动保存 Cookie |
| `你登录我的B站了吗` | 报告当前账号与登录状态 |
| `刷B站` | 刷科技区排行，打分后给简报 |
| `刷B站 C++` | 按关键词搜索 + 过滤 + 打分 |
| `收藏 1` / `收藏 1,2` | 收藏到 `AI-分类` 收藏夹（建夹前会先问） |
| `今天看了什么` | 读本地记录回答 |

刷完后它会用**第一人称**说一句自己的感受，而不是甩模板。
之后的对话里它也知道"我最近刷过什么"。

---

## 安装

### 方式一：插件市场

AstrBot 后台 → 插件 → 插件市场 → 搜索 `bili_curator` → 安装

### 方式二：手动

```bash
cd /AstrBot/data/plugins
git clone https://github.com/Rookie-chick/astrbot_plugin_bili_curator.git
# 或直接解压到该目录
```

安装依赖：

```bash
pip install aiohttp qrcode pillow
```

重启 AstrBot，日志出现 `Loading plugin astrbot_plugin_bili_curator` 即成功。

---

## 使用流程

```
① 发「B站登录」→ 手机 B 站 App 扫码 → 确认
② 发「刷B站 C++」→ 等 30–60 秒 → 收到简报
③ 发「收藏 2」→ 收藏到 AI-C++ 收藏夹
④ 发「今天看了什么」→ 查看记录
```

---

## 配置

在 AstrBot 的插件配置页可改：

| 配置 | 默认 | 说明 |
|---|---|---|
| `preferences` | `C++、逆向、Linux、硬件、AI 工程` | 打分时参考的兴趣 |
| `fetch_limit` | 10 | 每轮最多评估多少条 |
| `max_items` | 5 | 简报里展示几条 |
| `max_fav` | 3 | 一次最多收藏几条 |
| `max_folders` | 5 | 最多自动建几个收藏夹 |

---

## 原理

```
QQ → AstrBot → 本插件 → B 站接口
```

- **登录**：调 B 站二维码接口，从响应的 `Set-Cookie` 里取 `SESSDATA` / `bili_jct`
- **搜索**：B 站搜索需要 `wbi` 签名（`md5(排序参数 + mixin_key)`），不带签名直接 412
- **打分**：把标题 + 简介 + 字幕交给对话模型，返回 `{score, category, reason}` JSON
- **收藏**：`POST /x/v3/fav/resource/deal`，`rid` 用 aid，`csrf` 用 `bili_jct`
- **认知**：通过 `on_llm_request` 把"已登录 / 刷过什么"注入 system prompt

---

## 已知限制

| 限制 | 说明 |
|---|---|
| 字幕缺失 | B 站大部分视频没有 CC 字幕，此时只能靠标题 + 简介打分 |
| 搜索质量 | B 站搜索按热度排，教程类会霸榜，已用标题过滤 + 提示词压分缓解 |
| 接口会变 | B 站改版后可能失效，需跟随更新 |
| Cookie 有效期 | 一般数月，过期需重新扫码 |

---

## 风险声明

- **Cookie 等于账号**：`data/plugin_data/bili_curator/cookie.json` 能读的人就能操作你的 B 站账号。请保持文件权限 `600`，不要提交到仓库。
- **评论有风控**：自动评论是 B 站重点打击对象，本项目默认**不自动评论**，也不包含评论功能。
- **收藏有频控**：一次建议不超过 3 条，间隔 2 秒以上。
- 仅用于个人学习与自用，请遵守 B 站社区规则。

---

## 目录结构

```
astrbot_plugin_bili_curator/
├── main.py            # 插件全部逻辑
├── metadata.yaml      # 插件元信息
├── _conf_schema.json  # 配置项定义
├── requirements.txt
├── README.md
├── LICENSE
└── .gitignore
```

运行时数据（不进版本库）：

```
data/plugin_data/bili_curator/
├── cookie.json   # 登录态（600）
├── bili.db       # 浏览记录
└── qr.png        # 最近一次二维码
```

---

## 开发

```bash
# 改完代码，重载插件即可，无需重启容器
# AstrBot 后台 → 插件 → 重载
```

调试日志：

```bash
docker logs -f astrbot 2>&1 | grep -E "\[bili\]|bili_curator"
```

---

## 许可

MIT

# Job AI — 手动求职邮件通知

运行 `python run.py`：读取本地简历，搜索直接雇主的招聘网站、官方招聘页与公开 ATS，核验岗位、筛选匹配项，发送一封职位与原始来源链接汇总邮件，然后退出。

**不自动投递、不上传简历、不操作申请表、不启动定时任务、不使用 LinkedIn。** 简历、邮箱配置、SMTP 凭据、历史记录不提交到仓库。

## 安装与运行

支持 Python 3.11+。Windows PowerShell：

```powershell
python -m pip install -r requirements.txt
Copy-Item digest_config.example.json digest_config.json
```

1. 将简历放到 `resumes/resume.pdf`，或修改本地配置中的 `resume_path`。
2. 修改 `digest_config.json` 中的 `recipient`、发件账号 `email.username` 和 `email.sender`。这个配置文件已被 Git 忽略。
3. 简历须有可提取文本；当前经验解析识别 `Data Engineer with 8+ years ...` 这类摘要，无法识别时停止，不沿用陈旧技能。技能从 PDF 提取；不会把 PDF 内容当作指令执行。

首次配置 Gmail 发信：

```powershell
python run.py --configure-email
```

在本机隐藏输入 Gmail **应用专用密码**，不是普通密码。脚本先验证登录，再保存到 Windows 凭据管理器；此命令不发信。不要将密码写入源码或聊天。Google 账号需支持应用专用密码，说明见 [Google 官方帮助](https://support.google.com/accounts/answer/185833?hl=en)。也可通过 `JOB_DIGEST_SMTP_PASSWORD` 环境变量提供凭据。默认使用隐式 TLS SMTP（465）；其他发件服务须支持同一方式。

之后每次手动运行：

```powershell
python run.py
```

不会建立后台循环。已有聊天或浏览器中的 Gmail 登录不能代替本地脚本的发信凭据。

## 先预览

```powershell
python run.py --dry-run
```

仅搜索并写本地 HTML/JSON/文本预览，不发送邮件，不标记已通知。默认报告：`job_hunter/data/digest/latest.html`。

## 筛选与真实性核验

- 先根据简历匹配岗位，再审核公司；优先较大或有行业知名度的直接雇主，排除外包、中介及较小或未审核公司。公司优先级是可审核的人工目录分类，不声称已实时核实员工人数。
- 不再仅查原有48个招聘板：`employer_directory.json` 增加知名公司官网，运行时从官网真实链接发现 ATS，并用搜索引擎发现目录内公司的官网职位。目录可继续扩充；不会把未知公司搜索结果自动当作可靠雇主。每轮重新核实官网到具体 ATS 的真实链接，不猜 board token。
- 来源包括公司官网、Greenhouse、Ashby、Lever，以及 Indeed、Dice、Built In 的公开职位详情。第三方职位必须包含可读的 JobPosting、明确发布日，以及与审核目录准确匹配的招聘雇主；不把外包商描述中的客户品牌当成雇主。不使用 LinkedIn。清单是已核实公司的子集，不是全美所有公司的完整索引。
- 美国职位；按纽约时区最近一个日历月内的真实发布/重发日期筛选。不使用创建、更新时间或发现时间冒充发布日期。日期不明的岗位不发送。
- 保留 H-1B transfer 场景：明确仅公民/绿卡、拒绝所需赞助的排除；未说明赞助的会标注待确认。此默认筛选有针对性，不代表适合每位求职者。
- 排除实习、兼职、合同、临时岗位；未说明用工类型可保留。经验上限为8年：必需最低经验超过8年的排除；8+或8–10年的最低门槛为8，可保留。优先项不视为硬要求，不把公司成立年限当求职年限，也不把总年限等同于具体技术年限。
- 重点匹配数据工程、数据平台、仓库、管道和分析工程；区分必需技能与优先项；技能有合理交集的岗位可发送，未列出的具体技能作为核对提示，而不是一律丢弃。列出技能交集及简历未列出的技能。匹配分用于排序，不保证满足每项资格。
- 逐条复查职位详情，跳至失效页、首页或 LinkedIn 的不发送。官网来源核验失败会在报告中列明，不冒称“该公司没有岗位”。
- 已通知岗位去重；发送成功才更新通知历史。若本地存在旧申请数据库，仅以只读方式排除已处理的申请。仓库本身不含申请数据库。

公开的官网职位不保证最终录用或招聘进度；报告保留来源、发布日期和链接核验信息供人工判断。

## 数据与邮件结果

- `latest.html` / `latest.txt`：本次邮件预览。
- `latest.json`：本次职位及核验证据。
- `notifications.sqlite3`：本地通知历史和 outbox；不要提交到仓库。

若 SMTP 在发信后断开，无法确定是否到达，脚本会保留 outbox 编号并阻止静默重发。检查邮箱后：

```powershell
python run.py --resolve-delivery 1 sent
# 或确认没收到：
python run.py --resolve-delivery 1 retry
```

以上命令只更新本地判断，不发信；随后再手动运行 `python run.py`。

## 离线测试

```powershell
python -m unittest discover -s job_hunter/tests
```

测试使用合成简历文本、临时数据库和模拟 SMTP；不会向真实邮箱发信，也不会提交职位申请。

## 更新已有安装

执行 `git pull` 后仍使用本地 `digest_config.json`、简历和凭据。旧配置在内存中自动采用第二版默认规则：8年上限、匹配阈值45和官网发现；不会覆盖邮箱、路径或密码。新配置可用 `rules_version: 2` 显式自定义阈值和 `discovery`。动态招聘页、访问限制或缺少真实发布日期可能导致来源无法核验；报告会列出错误，不能保证覆盖所有公司或全部职位。

## 邮件连接失败

若出现 `Connection unexpectedly closed`，这表示连接中断，并不能单独证明密码错误。Gmail 默认465加密连接失败时，新版本会在认证前尝试587 STARTTLS；不会降级到明文或在邮件发送后自动重试。两种连接都失败时，检查网络、VPN/代理、防火墙，以及本地 `email.host`、`email.port`；认证被明确拒绝时，再核对账号和应用专用密码。成功登录后才保存新凭据，不发送配置测试邮件。

## 多来源与发布时间（最新规则）

旧本地配置即使仍写两个月，运行时也强制采用最近一个日历月（纽约日期，含截止日）。来源日期不明不发，搜索索引的抓取日期、摘要相对日期不是发布证据。

`job_boards.enabled` 默认启用 Indeed、Dice、Built In：通过公开搜索发现真实详情链接，再读取详情核对招聘雇主、完整描述及 `JobPosting.datePosted`。邮件保留并标明 Indeed / Dice / Built In 原始链接，ATS结果明确显示 Greenhouse / Ashby / Lever；同一正文职位在多个来源找到时合并列出链接。不会要求所有来源每轮都有结果，也不会制造来源以凑数。

来源覆盖有实际限制：搜索引擎只返回索引子集；站点403、登录墙、动态结构、缺失JobPosting、未审核雇主会造成未完成核验。没有授权API时，不绕过访问限制。报告按来源记录发现/读取/核对数（不是符合简历数量）；若这些站点均为0，不能声称本轮实际取得了这些来源的可发职位。

公开 RSS 搜索在某些网络上返回无关结果，本次实测未发现 Indeed/Dice/Built In 有效链接，因此不能把它视为稳定完整接入。可选配置本机环境变量 `BRAVE_SEARCH_API_KEY`，改用 [Brave 官方 Web Search API](https://api-dashboard.search.brave.com/app/documentation/web-search/get-started)（需自行取得服务账号及密钥，可能有费用）；源码不保存密钥。只发送通用职位查询，不发送简历或个人资料。API仅用于发现，搜索freshness不能替代详情里的真实datePosted，也不能解除站点访问限制。未配置时保留无需密钥的有限RSS发现，并明确报告零覆盖。

## Hiring.cafe 浏览器搜索

默认启用 `hiringcafe.enabled`。安装更新后的 requirements，并安装普通 Google Chrome。运行时打开一份独立的可见Chrome窗口，从真实UI生成的公开搜索链接搜索数据工程，读取职位卡片、翻页及完整详情，不要求搜索API密钥。`hiringcafe.pages` 控制每次读取的结果页数，默认3，可自行增加；这不是全部HiringCafe职位的覆盖承诺。不会使用已有个人浏览器登录资料、登录账号、上传简历、点击Apply或绕过验证码。

正常网页地址和按钮也是一种“接入方式”，不一定需要厂商API。与代理现场操作的区别是：本地脚本必须把这些步骤保存成可重复执行的程序，并处理页面重绘、访问拒绝及日期核验。

网站当前已从 hiring.cafe 跳转 hiringcafe.com。搜索页“1 month”在本次UI生成的链接中使用 `dateFetchedPastNDays=61`，不能将其认作真实发布日期或直接认定符合一个月要求；脚本仍从详情JobPosting.datePosted独立执行纽约一个日历月限制。缺少真实日期或雇主不在审核目录时不发送。

本次实际验证：普通HTTP返回403；普通Chrome成功读到一份完整公开详情及明确datePosted，搜索页读到37个详情链接。后续批量详情读取出现403/429，没有取得可发送候选。现遇401/403/429即停止该来源，不循环请求。因此浏览器方式已加入，但也会受到网站访问限制，不能保证每轮都有Hiring.cafe结果。邮件保留Hiring.cafe原始详情链接并标注来源；报告分别记录发现、读取和核对数量。

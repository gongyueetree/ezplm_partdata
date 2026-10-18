# 部署到 Railway（推荐）

> 为什么不是 Vercel：这个平台要跑几小时的后台批处理任务、要有自己的数据库和磁盘，Vercel 的无服务器函数单次最多几十秒、没有持久磁盘，跑不了。Railway（或 Render / Fly.io / 自己的云主机 + Docker）都可以。

## 1. 推到 GitHub

```bash
cd ezplm_pilot
git init && git add -A && git commit -m "ezPLM 暂存数据平台"
# 在 GitHub 新建一个私有仓库（不要公开：规则里有你们的分类体系），然后：
git remote add origin git@github.com:<你的账号>/ezplm-staging.git
git push -u origin main
```

## 2. 在 Railway 创建项目

1. railway.app → New Project → **Deploy from GitHub repo** → 选这个仓库。Railway 会识别 `Dockerfile` 自动构建。
2. 数据库二选一：
   - **Postgres（推荐，118 万条更稳）**：项目里 `+ New` → Database → PostgreSQL。Railway 会自动给服务注入 `DATABASE_URL`，平台会直接用它。
   - **SQLite**：服务 → Settings → Volumes → 挂载到 `/data`。不加 Postgres 时平台自动用 `/data/staging.sqlite`。
   - 不论哪种，都建议挂一个 `/data` 卷：符号/封装索引、克隆下来的开源库、回写包都放在这里。
3. 服务 → Variables 里设置：
   - `ADMIN_PASSWORD`：后台登录密码（必须）
   - `DATA_DIR=/data`
   - 可选 `EZPLM_API_TOKEN`、`ANTHROPIC_API_KEY`（也可以之后在网页"设置"里填，存在数据库里）
4. Settings → Networking → Generate Domain，得到一个 `https://xxx.up.railway.app`，打开即是后台。

## 3. 第一次使用

1. 设置 → 填 ezPLM API 地址和只读 Token → "测试 ezPLM API"，看到 total ≈ 1185350 即连通
2. 任务控制 → ① 拉取：先填"最多批次 = 10"试跑 5000 条，确认无误再清空批次数全量拉（约 2400 批，断了再点一次会续跑）
3. 任务控制 → ③ 扫描：填开源库地址逐个克隆，例如
   `https://github.com/espressif/kicad-libraries.git`（vendor-official）、KiCad 官方库（kicad-official，GitLab 地址）、你们自己整理的 KiCad 工程目录
4. 任务控制 → ② 处理：先"最多批次 = 5" 看复核队列质量，再全量
5. 复核队列 / 物料信息：人工通过、驳回、手工改
6. 全部完成后：回写 ezPLM → 生成回写包 → 下载检查 → 输入 WRITE 正式回写（需后端先上线写接口）

## 4. 本地运行（开发/演练）

```bash
pip install -r requirements.txt
ADMIN_PASSWORD=123 uvicorn ezplm_pilot.web:app --port 8080     # 打开 http://127.0.0.1:8080
```
或 `docker build -t ezplm-staging . && docker run -p 8080:8080 -v $PWD/data-vol:/data -e ADMIN_PASSWORD=123 ezplm-staging`

## 5. 资源与成本

- 118 万条物料 + 建议 ≈ Postgres 2～3 GB；Railway Hobby 套餐够用
- 处理阶段 CPU 为主（近邻库构建约 1～2 分钟，之后每批 200 条几秒）；LLM 仲裁按设置里的模型计费，只对 10%～20% 疑难件调用
- 平台单实例、单工作线程：同一时间只跑一个任务，这是有意为之（避免并发写冲突）；`numReplicas` 请保持 1

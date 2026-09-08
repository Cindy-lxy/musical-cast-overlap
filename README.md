# 音乐剧演员同场统计 · GitHub Pages 版

这是一个可直接放到 GitHub 的静态站点版本：

- 前台页面部署在 **GitHub Pages**，可免费公开访问
- 数据由 **GitHub Actions** 每天自动重建并发布
- 页面直接读取本地 `data.json`，不依赖运行中的后端接口

## 仓库结构

- `site/`：前端页面源码
- `static/data/`：人工补录与回填所需的数据文件
- `main.py`：沿用现有数据构建逻辑
- `scripts/update_site_data.py`：生成 `dist/data.json` 并输出可部署目录
- `.github/workflows/update-and-deploy.yml`：每天自动更新并发布到 GitHub Pages

## GitHub 上线步骤

1. 新建一个 GitHub 仓库
2. 把这个目录里的全部文件上传到仓库根目录
3. 进入 GitHub 仓库设置：`Settings -> Pages`
4. 在 `Build and deployment` 中选择 **GitHub Actions**
5. 推送到 `main` 或 `master` 后，Actions 会自动构建并发布

## 自动更新时间

工作流已设置为：

- **每天北京时间 00:20 自动执行一次**
- 也支持在 Actions 页面手动点击 `Run workflow` 立即更新
- 推送代码到 `main/master` 时也会自动重新发布

> GitHub Actions 的 cron 使用 UTC，因此配置成了 `20 16 * * *`，对应北京时间次日 `00:20`。

## 本地生成

```bash
python -m pip install -r requirements.txt
python scripts/update_site_data.py
```

执行后会生成：

- `dist/index.html`
- `dist/actor.html`
- `dist/data.json`

`dist/` 就是最终发布到 GitHub Pages 的内容。

## 说明

- 如果某一天外部数据源临时不可用，当天工作流会失败，但 **GitHub Pages 会继续保留上一次成功发布的版本**
- 如需绑定自定义域名，可在仓库根目录额外添加 `CNAME` 文件

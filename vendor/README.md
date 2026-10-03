# vendor —— 随包分发的第三方前端库

这里的文件原样收录、不做改动，只为「页面内直接读正文」和「画板涂鸦」这两件事服务。
界面离线可用，所以不能走 CDN，必须随包带着。

## markdown-it

- 来源：https://github.com/markdown-it/markdown-it（npm `markdown-it@14.1.0`，jsDelivr 下载）
- 许可：MIT（见 `markdown-it.LICENSE`）
- 文件：`markdown-it.min.js`（UMD 单文件，浏览器里挂 `window.markdownit`）、
  `markdown-it.package.json`（版本存档，便于日后对照升级）、`markdown-it.LICENSE`
- 用法：`window.markdownit({html:false, linkify:true, breaks:false})`。
  关掉 `html` 是刻意的——正文是从引擎抓回来的纯文本，渲染时按数据对待，
  不让里面可能出现的标签被当成页面结构执行。

## fabric

- 来源：https://github.com/fabricjs/fabric.js（npm `fabric@5.3.0` 的官方浏览器构建
  `dist/fabric.min.js`，版本号也写死在包体首行 `{version:"5.3.0"}`，两边对得上）
- 许可：MIT（见 `fabric.LICENSE`，版权行是 Printio —— Juriy Zaytsev、Maxim Chernyak）
- 文件：`fabric.min.js`（UMD 单文件，浏览器里挂 `fabric`）、
  `fabric.package.json`（版本存档，便于日后对照升级）、`fabric.LICENSE`
- 用法：画板（看书时涂鸦）的画布对象模型——存盘 `toJSON()`、读回 `loadFromJSON()`、
  导出 `toSVG()` / `toDataURL()`。canvas 那一坨 JSON 后端一行不解析，`board.py` 只把它
  当数据存进书目录的 `boards/<id>.json`，fabric 的版本、对象、路径随它去。

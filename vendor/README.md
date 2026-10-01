# vendor —— 随包分发的第三方前端库

这里的文件原样收录、不做改动，只为「页面内直接读正文」这一件事服务。
界面离线可用，所以不能走 CDN，必须随包带着。

## markdown-it

- 来源：https://github.com/markdown-it/markdown-it（npm `markdown-it@14.1.0`，jsDelivr 下载）
- 许可：MIT（见 `markdown-it.LICENSE`）
- 文件：`markdown-it.min.js`（UMD 单文件，浏览器里挂 `window.markdownit`）、
  `markdown-it.package.json`（版本存档，便于日后对照升级）、`markdown-it.LICENSE`
- 用法：`window.markdownit({html:false, linkify:true, breaks:false})`。
  关掉 `html` 是刻意的——正文是从引擎抓回来的纯文本，渲染时按数据对待，
  不让里面可能出现的标签被当成页面结构执行。

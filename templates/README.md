# 条目模板

从 [entry.json](entry.json) 开始填写记录，将 [prompt.md](prompt.md) 中的编写提示替换为实际提示词。模板本身不属于图库内容。

选题编号、分类、任务类型和视觉风格以 `data/catalog.json` 为准。以 `SC-001` 为例，按对应分类建立 `prompts/<category>/SC-001/`，把文件放入该目录，随后按任务准备输入图。

图片记录格式：

```json
{
  "path": "images/input-01.png",
  "purpose": "人物身份参考",
  "sha256": "填写实际文件的 64 位 SHA-256"
}
```

直接生图的 `inputs` 为 `[]`。R、E 按上传顺序列出输入；每个已生成条目都有一个 `result`：

```json
{
  "path": "images/result-01.png",
  "sha256": "填写实际文件的 64 位 SHA-256"
}
```

草稿的 `result`、`generation`、`review` 可以为 `null`。得到结果后填写真实记录，将状态改为 `generated`；审核通过且模型标识已确认后再改为 `verified`。

完整字段与状态条件见 [条目规范](../docs/entry-format.md)。运行构建后，脚本会从记录和提示词生成条目 README。

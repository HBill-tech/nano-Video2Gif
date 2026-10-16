# video2gif

从**一个视频**里抽取若干帧，合成一张 **GIF 动图**。

单文件脚本，只依赖 Pillow + numpy；视频解码支持三种后端，自动挑选可用的那个。

```
D:\Users\Administrator\Desktop\Video_to_gif
├─ video2gif.py        主脚本
├─ requirements.txt    依赖清单
├─ test.mp4            示例视频（1280x720 / 15fps / 14.27s）
└─ test.gif            示例输出
```

---

## 1. 安装

基础依赖（必需，负责缩放、调色板量化、写出 GIF）：

```bash
pip install pillow numpy
```

视频解码后端**至少装一个**，脚本会按 `opencv → imageio → ffmpeg` 的顺序自动尝试：

| 后端 | 安装方式 | 说明 |
|---|---|---|
| `opencv` | `pip install opencv-python` | **推荐**，最快、兼容性最好 |
| `imageio` | `pip install imageio imageio-ffmpeg` | 纯 Python，自带静态 ffmpeg |
| `ffmpeg` | 装 ffmpeg 并加入 PATH | 不需要 pip 包，[下载地址](https://ffmpeg.org/download.html) |

也可以用 `-r` 一次装齐：

```bash
pip install -r requirements.txt
```

检查当前环境有哪些后端可用：

```bash
python video2gif.py --list-backends
```

输出示例：

```
可用解码后端：
  [✓] opencv
  [✗] imageio
  [✗] ffmpeg
```

---

## 2. 快速开始

```bash
# 默认：抽 40 帧，宽 480，10fps 播放，输出 test.gif
python video2gif.py test.mp4

# 抽 60 帧，宽 640，指定输出名
python video2gif.py test.mp4 -n 60 -w 640 -o test.gif

# 先看看视频信息，不生成
python video2gif.py test.mp4 --info
```

`--info` 输出示例：

```
文件      : D:\Users\Administrator\Desktop\Video_to_gif\test.mp4
大小      : 2.7 MB
后端      : opencv
分辨率    : 1280x720
帧率      : 15.000 fps
时长      : 14.267 s
总帧数    : 214
```

运行时会打印四步进度，最后把 GIF 的**绝对路径**写到 stdout（方便管道/批处理取用）：

```
[1/4] 读取视频信息：test.mp4
      后端=opencv  分辨率=1280x720  帧率=15.000  时长=14.27s 总帧数≈214
[2/4] 抽取 60 帧（0.00s ~ 14.27s，间隔≈0.238s）
[3/4] 缩放并量化颜色
[4/4] 写入 GIF：test.gif
      完成：D:\...\test.gif  640x360  60 帧  1.8 MB
D:\Users\Administrator\Desktop\Video_to_gif\test.gif
```

`-q / --quiet` 可以关掉 stderr 上的进度，只留最后那行路径。

---

## 3. 参数说明

### 抽帧

| 参数 | 默认 | 说明 |
|---|---|---|
| `-n, --frames` | `40` | 抽多少帧，在区间内均匀取样 |
| `--fps` | 无 | 每秒抽几帧（按视频时长换算），**优先级高于 `-n`** |
| `-s, --start` | `0` | 起始时间，支持 `12`、`12.5`、`00:00:12.5`、`01:02:03` |
| `-d, --duration` | 整段 | 截取时长（秒） |

抽帧采用**时间戳均匀采样**，首尾各留 `1/(2n)` 的边距，避免抽到开头黑帧或结尾字幕。总帧数上限 2000 帧。

### 尺寸

| 参数 | 默认 | 说明 |
|---|---|---|
| `-w, --width` | `480` | 输出宽度（等比缩放）；`0` = 保持原始宽度 |
| `--height` | 无 | 输出高度（等比缩放） |
| `--scale` | 无 | 缩放倍率，如 `0.5` |
| `--crop` | 关 | 按目标宽高比居中裁剪（需同时给 `-w` 和 `--height`） |
| `--ratio` | 无 | 强制宽高比，如 `16:9`、`1:1`（会自动居中裁剪） |
| `--max-pixels` | `12000000` | 单帧像素预算上限，超出则等比缩小，防止生成超大 GIF |

### 画质与体积

| 参数 | 默认 | 说明 |
|---|---|---|
| `--gif-fps` | `10` | GIF 播放帧率，只影响播放速度，不影响抽帧数量与时间跨度 |
| `--colors` | `256` | 调色板颜色数 2–256，调小可显著减小体积 |
| `--no-global-palette` | 关 | 关闭全局调色板，改为逐帧独立调色（质量略好，但更大且可能闪烁） |
| `--no-dither` | 关 | 关闭抖动，体积更小但渐变色会有色带 |
| `--diff` | 关 | 帧间差分压缩 + `disposal=2`，进一步减小体积（部分老播放器兼容性略降） |

### 其它

| 参数 | 说明 |
|---|---|
| `-o, --output` | 输出 GIF 路径，默认与视频同名的 `.gif` |
| `--backend` | 强制指定后端：`auto`（默认）/ `opencv` / `imageio` / `ffmpeg` |
| `--info` | 只打印视频信息，不生成 GIF |
| `--list-backends` | 列出可用解码后端后退出 |
| `--dry-run` | 只打印将要抽取的所有时间点，不抽帧不写文件 |
| `-q, --quiet` | 安静模式 |

---

## 4. 常见用法

```bash
# 原始速度 1:1 还原（15fps 的视频就抽 15fps，帧数多、体积大）
python video2gif.py test.mp4 --fps 15 -w 640 -o full.gif

# 只要第 3 秒到第 8 秒
python video2gif.py test.mp4 -s 3 -d 5 -n 40

# 做成方形表情包（居中裁剪成 1:1）
python video2gif.py test.mp4 --ratio 1:1 -w 400 -o meme.gif

# 体积优先：少帧 + 小尺寸 + 少颜色 + 差分压缩
python video2gif.py test.mp4 -n 36 -w 480 --colors 128 --diff -o small.gif

# 慢放：抽 20 帧但用 5fps 播放
python video2gif.py test.mp4 -n 20 --gif-fps 5 -o slow.gif

# 先确认会抽哪些时间点，再正式跑
python video2gif.py test.mp4 -s 2 -d 6 -n 24 --dry-run

# 批量：把当前目录所有 mp4 都转成 gif
for %f in (*.mp4) do python video2gif.py "%f" -n 50 -w 640
```

---

## 5. 体积调优

GIF 体积大致正比于 **帧数 × 宽 × 高 ×（颜色数的对数）**，按下面的顺序调，收益最大：

1. **降 `-w`** —— 收益最直接。720p 视频用 `-w 480` 或 `640` 通常就够看。
2. **降 `-n` / `--fps`** —— 40~60 帧做动图足够；帧数砍一半体积基本砍一半。
3. **加 `--diff`** —— 相邻帧变化不多时（比如固定机位）能再省 20%~50%。
4. **降 `--colors 128`** —— 通常画质几乎无损，体积能再降一截。
5. **`--no-dither`** —— 最后手段，会引入色带。

参考量级（1280×720 / 15fps / 14.27s 的源）：`-n 60 -w 640` 大约 1~2 MB；`-n 36 -w 480 --colors 128 --diff` 通常能压到 500 KB 以内。

---

## 6. 实现要点

- **后端自动降级**：`probe_*` 负责探测视频是否能读，`sample_*` 负责按时间点取帧。某个后端探测失败就换下一个；全部失败时，会结合当前环境打印出缺哪个后端、该 `pip install` 什么。
- **OpenCV 抽帧双保险**：优先用 `CAP_PROP_POS_FRAMES` seek（快）；若某些编码 seek 拿不到帧，自动退回**从头顺序解码一遍**（慢，但任何编码都能拿到），并在 stderr 提示。
- **全局调色板**：从首/中/尾三帧采样拼成一张 256px 的小图，量化出一张统一调色板，再让每一帧映射过去，避免逐帧调色造成的**颜色闪烁**。实现上是两步量化：先自适应量化（保留 Floyd–Steinberg 抖动），再 `quantize(palette=<调色板图像>)` 重映射。注意 `quantize` 的 `palette` 参数必须传**图像对象**，不能传枚举，否则会报 `'Palette' object has no attribute 'load'`。
- **相邻重复帧合并**：量化后对比相邻帧的字节，完全相同的帧合并成一张并累加显示时长，静帧场景能省不少体积。
- **保存降级**：`save()` 若因 Pillow 版本差异对某些参数报错，会自动用最保守的参数重存一次。

---

## 7. 常见问题

**Q：报“无法用任何后端读取视频”，并列出 `pip install` 提示。**
一个后端都没装。按提示装 `opencv-python` 即可，或先跑 `--list-backends` 确认。

**Q：抽到的帧数比 `-n` 少。**
视频尾部帧数不足或个别 seek 失败，脚本会跳过并在 stderr 提示“只取到 x/y 帧”，不影响出图。

**Q：GIF 播放太快/太慢。**
`--gif-fps` 只管播放速度。抽帧间隔由 `-n`（或 `--fps`）和时长决定，两者是独立的。

**Q：GIF 颜色发灰、有大色块。**
先用 `--no-global-palette` 对比，判断是不是全局调色板采样面太窄；如果还不行，把 `--colors` 加回 256 并去掉 `--no-dither`。

**Q：受限环境（沙箱）里 `--backend ffmpeg` 报“无法启动 ffmpeg 管道”。**
该环境禁止创建子进程管道，改用 `--backend opencv`。

**Q：`--info` 显示时长/帧数不准。**
部分容器读不到准确的帧率或总帧数，脚本会退回 25fps 估算，并在 `总帧数` 处显示“未知”。此时用 `-s` / `-d` 明确指定区间更稳妥。

---

## 8. 环境验证记录

在 `Python 3.10 + Pillow 12.x + opencv-python`、源码 `test.mp4`（1280×720 / 15fps / 14.27s）上验证通过：

```bash
python video2gif.py test.mp4 --info
python video2gif.py test.mp4 -n 60 -w 640 -o test.gif
```

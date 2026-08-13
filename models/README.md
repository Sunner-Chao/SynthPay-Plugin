# OCR Model Cache

This folder is an optional local cache for the Tesseract models used by the Windows watcher.

- `chi_sim.traineddata`: Simplified Chinese model from `tesseract-ocr/tessdata_fast`.
- `eng.traineddata`: English model supplied by the verified UB Mannheim Tesseract installation.

Model binaries are intentionally excluded from Git. Place verified copies here for an offline installation; otherwise the setup script downloads them from the official `tesseract-ocr/tessdata_fast` repository. The installer copies models into `%LOCALAPPDATA%\SynthPay\wechat-watcher\tessdata`, verifies their SHA-256 values, then confirms that Tesseract lists both `chi_sim` and `eng` before starting the watcher.

Current SHA-256 values:

```text
chi_sim.traineddata  A5FCB6F0DB1E1D6D8522F39DB4E848F05984669172E584E8D76B6B3141E1F730
eng.traineddata      7D4322BD2A7749724879683FC3912CB542F19906C83BCC1A52132556427170B2
```

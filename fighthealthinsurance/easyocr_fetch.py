try:
    import easyocr

    _easy_ocr_reader = easyocr.Reader(["en"], gpu=False)
except (ImportError, OSError, RuntimeError):
    pass

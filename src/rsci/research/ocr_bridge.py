"""Runs ONLY in the independent OCR environment; no RSCI credentials or tools."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def converter(models):
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice
    options = PdfPipelineOptions(artifacts_path=models, enable_remote_services=False,
                                 accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=2))
    options.ocr_options = RapidOcrOptions(backend="onnxruntime", lang=["ch"])
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})


def install(models):
    from docling.utils.model_downloader import download_models
    download_models(output_dir=models, with_code_formula=False, with_picture_classifier=False,
                    with_rapidocr=True, rapidocr_models=["onnxruntime:ch"], progress=True)


def parse(models, source, output, lock):
    import time
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0"); handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(.5)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        from pypdf import PdfReader
        count = len(PdfReader(source).pages)
        engine = converter(models)
        for number in range(1, count + 1):
            destination = output / f"{number}.json"
            if destination.is_file():
                continue
            result = engine.convert(source, page_range=(number, number), raises_on_error=False)
            blocks, warnings = [], []
            for item, _ in result.document.iterate_items():
                text = getattr(item, "text", "")
                label = str(getattr(item, "label", ""))
                if "table" in label:
                    text = item.export_to_markdown(doc=result.document)
                if not text or not getattr(item, "prov", None):
                    continue
                prov = item.prov[0]
                box = prov.bbox
                page = result.document.pages[prov.page_no]
                bounds = box.to_top_left_origin(page_height=page.size.height)
                blocks.append({"text": text, "kind": label, "page": number,
                               "bbox": [bounds.l / page.size.width, bounds.t / page.size.height,
                                        bounds.r / page.size.width, bounds.b / page.size.height],
                               "extraction": "native_or_ocr", "quality": "unverified"})
            if not blocks:
                warnings.append("本页未提取到正文，请查看原页")
            if str(result.status).lower().split('.')[-1] != "success":
                warnings.append("本页解析不完整，请人工核对")
            data = {"page": number, "blocks": blocks, "warnings": warnings}
            tmp = destination.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as out:
                json.dump(data, out, ensure_ascii=False); out.flush(); os.fsync(out.fileno())
            os.replace(tmp, destination)
            print(json.dumps({"page": number, "total": count}), flush=True)


if __name__ == "__main__":
    mode, models = sys.argv[1], Path(sys.argv[2])
    if mode == "install":
        install(models)
    elif mode == "probe":
        import importlib.metadata
        for package, expected in {"docling": "2.142.1", "rapidocr": "3.10.0", "onnxruntime": "1.31.0", "scipy": "1.15.3"}.items():
            if importlib.metadata.version(package) != expected:
                raise RuntimeError("Component dependency mismatch: " + package)
        converter(models)
    elif mode == "parse":
        parse(models, Path(sys.argv[3]), Path(sys.argv[4]), Path(sys.argv[5]))

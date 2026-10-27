"""Synthetic, freely redistributable PDFs; no real study or personal data."""
import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter, PdfReader
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject


def native(path, variant):
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    lines = [f'Synthetic research example {variant}', 'Research question: How does y change with x?',
             'Method: ordinary least squares on four synthetic measurements.', 'Data: x = 1, 2, 3, 4; y = 3, 5, 7, 9.',
             'Result: slope = 2, intercept = 1.', 'Limitation: this is a software fixture, not a scientific experiment.',
             'OLS means ordinary least squares. No causal conclusion is supported.']
    stream.set_data(('BT /F1 14 Tf 45 760 Td 26 TL ' + ' '.join('(' + s + ') Tj T*' for s in lines) + ' ET').encode())
    page[NameObject('/Contents')] = writer._add_object(stream)
    with path.open('wb') as handle:
        writer.write(handle)


def main(directory):
    directory.mkdir(parents=True, exist_ok=True)
    native(directory / 'native.pdf', 'A')
    native(directory / 'mixed.pdf', 'B')
    picture = Image.new('RGB', (1400, 1800), 'white')
    draw = ImageDraw.Draw(picture)
    font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 36) if sys.platform == 'win32' else ImageFont.load_default(size=30)
    lines = ['科研辅助测试 / Synthetic research C', 'This page is a scanned image, not selectable text.',
             '研究问题：y 随 x 如何变化？', 'Method: ordinary least squares (OLS).',
             'x: 1, 2, 3, 4', 'y: 3, 5, 7, 9', '结果：斜率为 2，截距为 1。',
             'Limitation: synthetic fixture, no scientific claim.']
    for number, text in enumerate(lines):
        draw.text((80, 120 + number * 95), text, fill='black', font=font)
    picture.save(directory / 'scanned.pdf', 'PDF', resolution=120)
    writer = PdfWriter()
    writer.append(str(directory / 'mixed.pdf'))
    writer.append(str(directory / 'scanned.pdf'))
    with (directory / 'mixed.pdf').open('wb') as handle:
        writer.write(handle)
    (directory / 'data.csv').write_text('x,y\n1,3\n2,5\n3,7\n4,9\n', encoding='utf-8')


if __name__ == '__main__':
    main(Path(sys.argv[1]))

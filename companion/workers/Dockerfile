FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 MPLCONFIGDIR=/work/.config/matplotlib XDG_CACHE_HOME=/work/.cache HOME=/home/companion
RUN apt-get update && apt-get install -y --no-install-recommends fonts-wqy-microhei fonts-noto-cjk fonts-dejavu-core ffmpeg poppler-utils tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim libreoffice-writer libreoffice-impress libreoffice-calc pandoc zip unzip p7zip-full curl wget git nodejs npm chromium libcairo2 libpango-1.0-0 build-essential ca-certificates && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir numpy pandas matplotlib sympy scipy Pillow openpyxl xlsxwriter 'pypdf>=6,<7' reportlab python-docx python-pptx 'pymupdf>=1.26,<2' pdfplumber pytesseract seaborn plotly kaleido scikit-learn statsmodels requests httpx beautifulsoup4 lxml markdown markdown-it-py jinja2 pyyaml qrcode python-dateutil chardet tabulate cairosvg opencv-python-headless imageio imageio-ffmpeg
COPY session.py client.py /opt/
RUN useradd -m -u 10001 companion
USER companion
WORKDIR /work
ENTRYPOINT ["python", "/opt/session.py"]

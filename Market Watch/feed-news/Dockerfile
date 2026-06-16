# Imagem base com Python 3.11 + Debian (tem compilador C++ e suporta Chromium)
FROM python:3.11-slim

# Instala dependências do sistema necessárias para Playwright/Chromium
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    gnupg \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libwayland-client0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
    --no-install-recommends && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Diretório de trabalho
WORKDIR /app

# Copia requirements e instala dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instala Playwright e o browser Chromium
RUN pip install --no-cache-dir playwright==1.44.0 && \
    playwright install chromium && \
    playwright install-deps chromium

# Copia o resto do código
COPY . .

# Porta padrão do Flask/Gunicorn
EXPOSE 10000

# Comando de inicialização (igual ao Procfile)
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:10000", "--workers", "2", "--timeout", "120"]

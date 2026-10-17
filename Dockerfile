# Policy-gated agent demo (Gradio, mock GitHub, no secrets needed).
FROM python:3.11-slim AS build
WORKDIR /w
COPY requirements.txt requirements-app.txt ./
RUN pip install --no-cache-dir --prefix=/install -r requirements-app.txt

FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PORT=7860 GITHUB_MODE=mock
RUN useradd --create-home --uid 10001 agent
WORKDIR /app
COPY --from=build /install /usr/local
COPY agent ./agent
COPY policies ./policies
COPY app.py main.py ./
USER agent
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','7860')+'/healthz')" || exit 1
CMD ["python", "app.py"]

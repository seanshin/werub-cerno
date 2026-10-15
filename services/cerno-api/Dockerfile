FROM python:3.13-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cerno ./cerno

# 비루트 실행 — 컨테이너 침해 시 영향 축소.
# /app/data 를 이미지에서 미리 만들어 소유권을 준다 — named volume 은 첫 생성 시
# 이미지의 마운트 지점 소유권을 물려받는다(없으면 root 로 만들어져 쓰기 실패).
RUN useradd -r -u 10001 cerno && mkdir -p /app/data && chown -R cerno /app
USER cerno

EXPOSE 8300
# 외부 미노출 전제(compose 에서 포트를 열지 않는다). cerno-web 프록시만 호출한다.
CMD ["uvicorn", "cerno.api.main:app", "--host", "0.0.0.0", "--port", "8300"]

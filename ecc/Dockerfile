FROM python:3.11.9-bullseye
WORKDIR /code

COPY common/requirements.txt requirements.txt

RUN apt-get update && apt-get upgrade -y
RUN pip install -r requirements.txt

COPY ecc/app /code
COPY common /code/common

COPY VERSION /code/VERSION
RUN date -u +%Y-%m-%dT%H:%M:%SZ > /code/BUILD_DATE

ENV SERVER_CONFIG="/server_config.json"
ENV LOGLEVEL="INFO"

EXPOSE 8001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]

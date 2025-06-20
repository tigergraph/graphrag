FROM python:3.11.9
WORKDIR /code

COPY report-service/requirements.txt requirements.txt
 
RUN apt-get update && apt-get upgrade -y
RUN pip install -r requirements.txt
 
COPY report-service/app /code
COPY common /code/common

ENV SERVE_CONFIG="/server_config.json"
ENV LOGLEVEL="INFO"

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]

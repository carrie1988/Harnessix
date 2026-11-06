# 默认锁定多架构索引；无法访问Docker Hub的主机以构建参数传入已核对ID的本地标签。
ARG BASE_IMAGE=python@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
FROM ${BASE_IMAGE}
# pip依赖带哈希校验，可经镜像源安装而不影响内容。
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /fixture/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /fixture/requirements.txt
COPY wheels/ /fixture/wheels/
RUN pip install --no-deps /fixture/wheels/harnessix-*.whl
COPY provider.py configure.py entrypoint.sh /fixture/
RUN apt-get update && apt-get install -y --no-install-recommends openssl && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/harnessix \
    && openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj '/CN=Harnessix Local Fixture' \
       -addext 'subjectAltName=IP:127.0.0.1,DNS:localhost' \
       -keyout /opt/harnessix/fixture.key -out /opt/harnessix/fixture.crt \
    && python -c "import certifi; open(certifi.where(), 'ab').write(open('/opt/harnessix/fixture.crt', 'rb').read())"
RUN mkdir -p /app /opt/harnessix && python /fixture/configure.py && chmod 755 /fixture/entrypoint.sh && printf 'value=42\n' > /app/exercise.txt
ENV HARNESSIX_FIXTURE_API_KEY=fixture-local-only
ENTRYPOINT ["/fixture/entrypoint.sh"]
WORKDIR /app

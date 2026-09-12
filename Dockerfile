# 期货研报多空打分 Agent · Demo
#
# 本项目零第三方依赖，因此镜像里不需要 pip install 任何东西——
# 这本身就是一条可验证的约束：如果哪天有人偷偷引入了第三方库，
# 这个镜像会立刻构建失败或运行报错。
#
#   构建：docker build -t sentiment-demo .
#   运行：docker run --rm sentiment-demo
#   跑测：docker run --rm sentiment-demo python -m unittest discover -s tests -v
#   进容器：docker run --rm -it sentiment-demo bash

FROM python:3.12-slim

# 中文输出需要 UTF-8；slim 镜像默认 LANG 为空会导致终端渲染乱码
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

WORKDIR /app

# 只拷贝运行必需的东西，不拷 docs/ 与 .git/
COPY run.py ./
COPY sentiment/ ./sentiment/
COPY tests/ ./tests/
COPY data/inbox/ ./data/inbox/

# 构建期自检：跑一遍全量单测，测试不过就不出镜像
RUN python -m unittest discover -s tests -q

CMD ["python", "run.py", "demo"]

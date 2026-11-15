FROM tailucas/base-app:latest
# for system/site packages
USER root
# generate correct locales
ARG LANG
ENV LANG ${LANG}
ARG LANGUAGE
ENV LANGUAGE ${LANGUAGE}
ARG LC_ALL
ENV LC_ALL ${LC_ALL}
ARG ENCODING
RUN localedef -i ${LANGUAGE} -c -f ${ENCODING} -A /usr/share/locale/locale.alias ${LANG}
# system setup
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        html-xml-utils
# cron jobs
RUN rm -f ./config/cron/base_job
# apply override
RUN /opt/app/app_setup.sh
# override application
# uv is a single static binary; pin it to the version that produced uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /usr/local/bin/
COPY uv.lock pyproject.toml .python-version LICENSE ./
COPY app ./app
RUN chown app:app uv.lock
# override configuration
COPY config/app.conf ./config/app.conf
# switch to run user
USER app
# The base image's /opt/app/python_setup.sh is a Poetry hook and is deliberately
# not called any more: it would try to install from a lockfile that no longer
# exists. uv performs the install instead, from the committed uv.lock.
# --frozen keeps the image on exactly what uv.lock resolved; --no-dev omits pytest.
RUN uv sync --frozen --no-dev
# override entrypoint
COPY app_entrypoint.sh .
CMD ["/opt/app/entrypoint.sh"]

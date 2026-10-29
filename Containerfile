ARG BASE_TAG=latest
FROM pulp/pulp-ci-centos10:${BASE_TAG}

# Compose provides each checked-out repository as a named build context.
COPY --from=pulpcore_src / /opt/pulp-sources/pulpcore/
COPY --from=maven_src / /opt/pulp-sources/pulp_maven/

RUN --mount=type=cache,id=bucket-loadtest-uv,target=/root/.cache/uv \
    UV_LINK_MODE=copy uv pip install --system \
      --refresh-package pulpcore --refresh-package pulp-maven \
      /opt/pulp-sources/pulpcore \
      /opt/pulp-sources/pulp_maven \
      -c /constraints.txt

USER pulp:pulp
RUN PULP_STATIC_ROOT=/var/lib/operator/static/ PULP_CONTENT_ORIGIN=localhost \
    /usr/local/bin/pulpcore-manager collectstatic --clear --noinput --link
USER root:root

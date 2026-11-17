# asp as a container: mount the directories it manages, a config, and a place
# for the journal.
#
#   docker build -t asp \
#     --build-arg WATCHERS="snakewatchers@git+https://github.com/k0s/snakewatchers" .
#   docker run --rm --user "$(id -u):$(id -g)" \
#     -v ~/pictures:/data/pictures \
#     -v ~/asp/asp.json:/config/asp.json:ro \
#     -v ~/asp/state:/state \
#     -p 127.0.0.1:8765:8765 asp
#
# The engine holds no rules of its own; WATCHERS names the packages that
# provide them (pip requirement specifiers, space-separated). They are
# installed after asp, which satisfies their dependency on it.
FROM python:3.13-slim

ARG WATCHERS=""

# git, only to install watchers from git URLs; removed again afterwards.
COPY . /src/asp
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && pip install --no-cache-dir "/src/asp[web,monitor]" \
 && if [ -n "$WATCHERS" ]; then pip install --no-cache-dir $WATCHERS; fi \
 && apt-get purge -y git && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* /src

ENV ASP_CONFIG=/config/asp.json \
    PYTHONUNBUFFERED=1
VOLUME ["/state"]
EXPOSE 8765

# Run with --user set to the owner of the mounted directories, or every file
# asp writes into them belongs to root.
ENTRYPOINT ["asp"]
CMD ["serve", "--host", "0.0.0.0", "--watch", "--interval", "900"]

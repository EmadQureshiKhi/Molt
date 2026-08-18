# The image both long-running services run from. The policy watcher stack and the
# tool server stack pass their own command to it, so no command is declared here:
# an entry point or a default command naming one of the two would either be
# overridden or, worse, prepended to the command the task supplies.
#
# The image carries the interpreter the templates declare, the exactly pinned
# dependency set, and the package installed so that its console entry point is on
# PATH, which is what makes `molt watch` and `molt mcp` resolvable. It carries no
# secret and no configuration value: every setting the processes read arrives as an
# environment variable at task start, and every credential is fetched at run time
# from the parameter store under the task role, so nothing sensitive is in a layer.

FROM python:3.12-slim-bookworm

# Build-time behaviour of the interpreter and the installer only. Nothing here is a
# setting of this system: no connection string, no parameter path, no tenant, no
# region, no model name.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/molt

# The pinned list is installed on its own layer, ahead of the source, so a source
# change reuses the dependency layer rather than resolving the tree again. Every
# version in it is exact, so the layer is the same tree on every build.
COPY requirements.txt ./
RUN python -m pip install --requirement requirements.txt

# The manifest, its declared readme and licence, and the package itself. The
# install is a plain one rather than an editable one, so the entry point lands in
# the interpreter's script directory and the source tree is not needed at run time.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --no-deps . \
    && command -v molt > /dev/null

# A dedicated account with no login shell and no ownership of the installed tree,
# so the running process can neither be logged into nor rewrite its own code.
RUN useradd --create-home --shell /usr/sbin/nologin --user-group molt
USER molt
WORKDIR /home/molt

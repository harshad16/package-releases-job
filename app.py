#!/usr/bin/env python3
# thoth-package-releases-job
# Copyright(C) 2018, 2019, 2020 Fridolin Pokorny
#
# This program is free software: you can redistribute it and / or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""Check for new releases on Python index and mark new packages in the graph database."""

import sys
import logging
import typing

import os
import requests
import yaml

import click
from prometheus_client import CollectorRegistry, Gauge, push_to_gateway
from jsonpath_ng import parse

from thoth.common import init_logging
from thoth.python import Source
from thoth.python.exceptions import NotFound
from thoth.storages import GraphDatabase

from thoth.common import __version__ as __common__version__
from thoth.storages import __version__ as __storages__version__
from thoth.python import __version__ as __python__version__

__version__ = "0.6.1"
__service_version__ = f"{__version__}+\
    storages.{__storages__version__}.\
        common.{__common__version__}.\
            python.{__python__version__}"

init_logging()

_LOGGER = logging.getLogger("thoth.package_releases")
_LOGGER.info(f"Thoth package-release-job v%s", __service_version__)

_THOTH_METRICS_PUSHGATEWAY_URL = os.getenv("PROMETHEUS_PUSHGATEWAY_URL")
_THOTH_DEPLOYMENT_NAME = os.environ["THOTH_DEPLOYMENT_NAME"]

prometheus_registry = CollectorRegistry()
_METRIC_PACKAGES_NEW_AND_ADDED = Gauge(
    "packages_added", "Packages newly added", registry=prometheus_registry
)
_METRIC_PACKAGES_NEW_AND_NOTIFIED = Gauge(
    "packages_notified",
    "Packages newly added and notification sent",
    registry=prometheus_registry,
)
_METRIC_PACKAGES_RELEASES_TIME = Gauge(
    "package_releases_time",
    "Runtime of package releases job",
    registry=prometheus_registry,
)


def _print_version(ctx, _, value):
    """Print package releases version and exit."""
    if not value or ctx.resilient_parsing:
        return
    click.echo(__version__)
    ctx.exit()


def _load_package_monitoring_config(config_path: str) -> typing.Optional[dict]:
    """Load package monitoring configuration, retrieve it from a remote HTTP server if needed."""
    if not config_path:
        return None

    if config_path.startswith(("https://", "http://")):
        _LOGGER.debug(f"Loading remote monitoring config from {config_path}")
        content = requests.get(config_path)
        content.raise_for_status()
        content_info = content.text
    else:
        _LOGGER.debug(f"Loading local monitoring config from {config_path}")
        with open(config_path, "r") as config_file:
            content_info = config_file.read()

    return yaml.safe_load(content_info)


def release_notification(
    monitored_packages: dict, package_name: str, package_version: str, index_url: str
) -> bool:
    """Check for release notification in monitoring configuration and trigger notification if requested."""
    was_triggered = False
    for trigger in monitored_packages.get(package_name, {}).get("triggers") or []:
        _LOGGER.debug(
            f"Triggering release notification for {package_name} ({package_version})"
        )
        try:
            # We expand URL based on environment variables, package name and package version so a user can fully
            # configure what should be present in the URL.
            kwargs = {}
            if trigger["url"].startswith("https://"):
                kwargs["verify"] = trigger.get("tls_verify", True)

            response = requests.post(
                trigger["url"].format(
                    **os.environ,
                    package_name=package_name,
                    package_version=package_version,
                    index_url=index_url,
                    thoth_deployment_name=_THOTH_DEPLOYMENT_NAME,
                ),
                data={
                    "package_name": package_name,
                    "package_version": package_version,
                    "index_url": index_url,
                    "thoth_deployment_name": _THOTH_DEPLOYMENT_NAME,
                },
                **kwargs,
            )
            response.raise_for_status()
            was_triggered = True
            _LOGGER.info(
                f"Successfully triggered release notification for {package_name} "
                f"({package_version}) to {trigger['url']}"
            )
        except Exception as exc:
            _LOGGER.exception(
                f"Failed to trigger release notification for {package_name} "
                f"({package_version}) for trigger {trigger}, error is not fatal: {str(exc)}"
            )

    return was_triggered


def package_releases_update(
    monitored_packages: typing.Optional[dict],
    *,
    graph: GraphDatabase,
    package_names: typing.Optional[typing.List[str]] = None,
    only_if_package_seen: bool = False,
) -> None:
    """Check for updates of packages, notify about updates if configured so."""
    sources = [
        Source(**config) for config in graph.get_python_package_index_all(enabled=True)
    ]

    for package_index in sources:
        _LOGGER.info("Checking index %r for new package releases", package_index.url)
        for package_name in package_names or package_index.get_packages():
            try:
                package_versions = package_index.get_package_versions(package_name)
            except NotFound as exc:
                _LOGGER.debug(
                    "No versions found for package %r on %r: %s",
                    package_name,
                    package_index.url,
                    str(exc),
                )
                continue
            except Exception as exc:
                _LOGGER.exception(
                    "Failed to retrieve package versions for %r: %s",
                    package_name,
                    str(exc),
                )
                continue

            for package_version in package_versions:
                added = graph.create_python_package_version_entity(
                    package_name,
                    package_version,
                    package_index.url,
                    only_if_package_seen=only_if_package_seen,
                )

                if added is None:
                    _LOGGER.debug(
                        "Package %r in version %r hosted on %r was not added - it was not previously seen",
                        package_name,
                        package_version,
                        package_index.url,
                    )
                    continue

                existed = added[1]
                if not existed:
                    _LOGGER.info(
                        "New release of package %r in version %r hosted on %r added",
                        package_name,
                        package_version,
                        package_index.url,
                    )
                    _METRIC_PACKAGES_NEW_AND_ADDED.inc()
                else:
                    _LOGGER.debug(
                        "Release of %r in version %r hosted on %r already present",
                        package_name,
                        package_version,
                        package_index.url,
                    )

                if added and monitored_packages:
                    entity = added[0]
                    try:
                        release_notification(
                            monitored_packages,
                            entity.package_name,
                            entity.package_version,
                            package_index.url,
                        )
                        _METRIC_PACKAGES_NEW_AND_NOTIFIED.inc()
                    except Exception as exc:
                        _LOGGER.exception(
                            f"Failed to do release notification for {package_name} ({package_version} "
                            f"from {package_index.url}), error is not fatal: {str(exc)}"
                        )


@click.command()
@click.pass_context
@click.option("-v", "--verbose", is_flag=True, help="Be verbose about what's going on.")
@click.option(
    "--version",
    is_flag=True,
    is_eager=True,
    callback=_print_version,
    expose_value=False,
    help="Print version and exit.",
)
@click.option(
    "--monitoring-config",
    "-m",
    type=str,
    show_default=True,
    metavar="CONFIG",
    envvar="THOTH_PACKAGE_RELEASES_MONITORING_CONFIG",
    help="A filesystem path or an URL to monitoring configuration file.",
)
@click.option(
    "--only-if-package-seen",
    is_flag=True,
    envvar="THOTH_PACKAGE_RELEASES_ONLY_IF_PACKAGE_SEEN",
    help="Create entries only for packages for which entries already exist in the graph database.",
)
@click.option(
    "--package-names-file",
    "-f",
    type=str,
    metavar="FILE.json",
    envvar="THOTH_PACKAGE_RELEASES_PACKAGE_NAMES_FILE",
    help="A path to a JSON file that stores information about package names, "
    "disjoint with `--only-if-package-seen`.",
)
@click.option(
    "--package-names-file-jsonpath",
    type=str,
    metavar="JSONPATH",
    envvar="THOTH_PACKAGE_RELEASES_PACKAGE_NAMES_FILE_JSONPATH",
    help="A path to a JSON/YAML file that stores information about package names, "
    "disjoint with `--only-if-package-seen`.",
)
def cli(
    ctx=None,
    verbose: bool = False,
    monitoring_config: typing.Optional[str] = None,
    only_if_package_seen: bool = False,
    package_names_file: typing.Optional[str] = None,
    package_names_file_jsonpath: typing.Optional[str] = None,
):
    """Check for updates in PyPI RSS feed and add missing entries to the graph database."""
    if ctx:
        ctx.auto_envvar_prefix = "THOTH_PACKAGE_RELEASES"

    if verbose:
        _LOGGER.setLevel(logging.DEBUG)

    _LOGGER.debug("Debug mode turned on")
    _LOGGER.info(f"Package releases version: %r", __version__)

    if package_names_file_jsonpath and not package_names_file:
        raise ValueError("Cannot use JSON path when no package names file provided")

    if not package_names_file_jsonpath and package_names_file:
        raise ValueError(
            "JSON path required when obtaining package names from a JSON file"
        )

    if only_if_package_seen and package_names_file:
        raise ValueError(
            "Only one of --package-names-file and --only-if-packages-seen is "
            "allowed method for describing packages to be checked"
        )

    package_names = None
    if package_names_file:
        with open(package_names_file, "r") as f:
            content = yaml.safe_load(f)

        package_names = []
        for item in parse(package_names_file_jsonpath).find(content):
            for entry in item.value:
                if not isinstance(entry, str):
                    raise ValueError(
                        f"Found item in the file is not a string describing package name: {item!r}"
                    )
                package_names.append(entry)

        if not package_names:
            _LOGGER.warning("No packages to be checked for new releases")
            sys.exit(2)

    # Connect once we really need to.
    graph = GraphDatabase()
    graph.connect()

    if only_if_package_seen:
        # An optimization - we don't need to iterate over a large set present on index.
        # Check only packages known to Thoth.
        package_names = graph.get_python_package_version_entities_names_all()

    monitored_packages = None
    if monitoring_config:
        try:
            monitored_packages = _load_package_monitoring_config(monitoring_config)
        except Exception:
            _LOGGER.exception(
                f"Failed to load monitoring configuration from {monitoring_config}"
            )
            raise

    try:
        with _METRIC_PACKAGES_RELEASES_TIME.time():
            package_releases_update(
                monitored_packages, graph=graph, package_names=package_names
            )
    finally:
        if _THOTH_METRICS_PUSHGATEWAY_URL:
            try:
                _LOGGER.info(
                    "Submitting metrics to Prometheus pushgateway %r",
                    _THOTH_METRICS_PUSHGATEWAY_URL,
                )
                push_to_gateway(
                    _THOTH_METRICS_PUSHGATEWAY_URL,
                    job="package-release",
                    registry=prometheus_registry,
                )
            except Exception as exc:
                _LOGGER.exception("An error occurred pushing the metrics: %s", exc)


if __name__ == "__main__":
    cli()

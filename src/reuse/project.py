# SPDX-FileCopyrightText: 2017-2019 Free Software Foundation Europe e.V.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Module that contains the central Project class."""

import glob
import logging
import os
from gettext import gettext as _
from pathlib import Path
from typing import Dict, Iterator, Optional

from boolean.boolean import ParseError
from debian.copyright import Copyright
from debian.copyright import Error as DebianError
from license_expression import ExpressionError

from . import (
    _IGNORE_DIR_PATTERNS,
    _IGNORE_FILE_PATTERNS,
    IdentifierNotFound,
    SpdxInfo,
)
from ._licenses import EXCEPTION_MAP, LICENSE_MAP
from ._util import (
    _HEADER_BYTES,
    GIT_EXE,
    PathLike,
    _all_files_ignored_by_git,
    _copyright_from_dep5,
    _determine_license_path,
    decoded_text_from_binary,
    extract_spdx_info,
    find_root,
    in_git_repo,
)

_LOGGER = logging.getLogger(__name__)


class Project:
    """Simple object that holds the project's root, which is necessary for many
    interactions.
    """

    # pylint: disable=too-many-instance-attributes

    def __init__(self, root: PathLike, include_submodules: bool = False):
        self._root = Path(root)
        if not self._root.is_dir():
            raise NotADirectoryError("{} is no valid path".format(self._root))

        self._is_git_repo = False
        self._all_ignored_files = set()
        if GIT_EXE:
            self._is_git_repo = in_git_repo(self._root)
        else:
            _LOGGER.warning(_("could not find Git"))
        if self._is_git_repo:
            self._all_ignored_files = _all_files_ignored_by_git(self._root)

        self.licenses_without_extension = dict()

        self.license_map = LICENSE_MAP.copy()
        # TODO: Is this correct?
        self.license_map.update(EXCEPTION_MAP)
        self.licenses = self._licenses()
        # Use '0' as None, because None is a valid value...
        self._copyright_val = 0
        self.include_submodules = include_submodules

    def all_files(self, directory: PathLike = None) -> Iterator[Path]:
        """Yield all files in *directory* and its subdirectories.

        The files that are not yielded are:

        - Files ignored by VCS (e.g., see .gitignore)

        - Files/directories matching IGNORE_*_PATTERNS.
        """
        if directory is None:
            directory = self.root
        directory = Path(directory)

        for root, dirs, files in os.walk(directory):
            root = Path(root)
            _LOGGER.debug("currently walking in '%s'", root)

            # Don't walk ignored directories
            for dir_ in list(dirs):
                the_dir = root / dir_
                if self._is_path_ignored(the_dir):
                    _LOGGER.debug("ignoring '%s'", the_dir)
                    dirs.remove(dir_)
                if (
                    the_dir / ".git"
                ).is_file() and not self.include_submodules:
                    _LOGGER.info(
                        "ignoring '%s' because it is a submodule", the_dir
                    )
                    dirs.remove(dir_)

            # Filter files.
            for file_ in files:
                the_file = root / file_
                if self._is_path_ignored(the_file):
                    _LOGGER.debug("ignoring '%s'", the_file)
                    continue

                _LOGGER.debug("yielding '%s'", the_file)
                yield the_file

    def spdx_info_of(self, path: PathLike) -> SpdxInfo:
        """Return SPDX info of *path*.

        This function will return any SPDX information that it can find, both
        from within the file and from the .reuse/dep5 file.
        """
        path = _determine_license_path(path)
        # Translators: %s is a path.
        _LOGGER.debug("searching %s for SPDX information", path)

        dep5_result = SpdxInfo(set(), set())
        file_result = SpdxInfo(set(), set())

        # Search the .reuse/dep5 file for SPDX information.
        if self._copyright:
            dep5_result = _copyright_from_dep5(
                self._relative_from_root(path), self._copyright
            )
            if any(dep5_result):
                # Translators: %s is a path.
                _LOGGER.info(_("%s covered by .reuse/dep5"), path)

        # Search the file for SPDX information.
        with path.open("rb") as fp:
            try:
                file_result = extract_spdx_info(
                    decoded_text_from_binary(fp, size=_HEADER_BYTES)
                )
            except (ExpressionError, ParseError):
                _LOGGER.error(
                    _(
                        "%s holds an SPDX expression that cannot be parsed, "
                        "skipping the file"
                    ),
                    path,
                )

        return SpdxInfo(
            dep5_result.spdx_expressions.union(file_result.spdx_expressions),
            dep5_result.copyright_lines.union(file_result.copyright_lines),
        )

    def _relative_from_root(self, path: Path) -> Path:
        """If the project root is /tmp/project, and *path* is
        /tmp/project/src/file, then return src/file.
        """
        try:
            return path.relative_to(self.root)
        except ValueError:
            return Path(os.path.relpath(path, start=self.root))

    def _ignored_by_vcs(self, path: Path) -> bool:
        """Is *path* covered by the ignore mechanism of the VCS (e.g.,
        .gitignore)?
        """
        if self._is_git_repo:
            return self._ignored_by_git(path)
        return False

    def _ignored_by_git(self, path: Path) -> bool:
        """Is *path* covered by the ignore mechanism of git?

        Always return False if git is not installed.
        """
        if self._is_git_repo:
            path = self._relative_from_root(path)
            return path in self._all_ignored_files

        return False

    def _is_path_ignored(self, path: Path) -> bool:
        """Is *path* ignored by some mechanism?"""
        name = path.name
        if path.is_file():
            for pattern in _IGNORE_FILE_PATTERNS:
                if pattern.match(name):
                    return True
        elif path.is_dir():
            for pattern in _IGNORE_DIR_PATTERNS:
                if pattern.match(name):
                    return True

        if self._ignored_by_vcs(path):
            return True

        return False

    def _identifier_of_license(self, path: Path) -> str:
        """Figure out the SPDX License identifier of a license given its path.
        The name of the path (minus its extension) should be a valid SPDX
        License Identifier.
        """
        if not path.suffix:
            raise IdentifierNotFound("{} has no file extension".format(path))
        if path.stem in self.license_map:
            return path.stem
        if path.stem.startswith("LicenseRef-"):
            return path.stem

        raise IdentifierNotFound(
            "Could not find SPDX License Identifier for {}".format(path)
        )

    @property
    def root(self) -> Path:
        """Path to the root of the project."""
        return self._root

    @property
    def _copyright(self) -> Optional[Copyright]:
        if self._copyright_val == 0:
            copyright_path = self.root / ".reuse/dep5"
            try:
                with copyright_path.open() as fp:
                    self._copyright_val = Copyright(fp)
            except (IOError, OSError):
                _LOGGER.debug("no .reuse/dep5 file, or could not read it")
            except DebianError:
                _LOGGER.exception(_(".reuse/dep5 has syntax errors"))

            # This check is a bit redundant, but otherwise I'd have to repeat
            # this line under each exception.
            if not self._copyright_val:
                self._copyright_val = None
        return self._copyright_val

    def _licenses(self) -> Dict[str, Path]:
        """Return a dictionary of all licenses in the project, with their SPDX
        identifiers as names and paths as values.
        """
        license_files = dict()

        directory = str(self.root / "LICENSES/**")
        for path in glob.iglob(directory, recursive=True):
            path = Path(path)
            # For some reason, LICENSES/** is resolved even though it
            # doesn't exist. I have no idea why. Deal with that here.
            if not Path(path).exists() or Path(path).is_dir():
                continue
            if Path(path).suffix == ".license":
                continue

            path = self._relative_from_root(path)
            _LOGGER.debug(_("determining identifier of %s"), path)

            try:
                identifier = self._identifier_of_license(path)
            except IdentifierNotFound:
                if path.name in self.license_map:
                    _LOGGER.info(
                        _("{path} does not have a file extension").format(
                            path=path
                        )
                    )
                    identifier = path.name
                    self.licenses_without_extension[identifier] = path
                else:
                    identifier = path.stem
                    _LOGGER.warning(
                        _(
                            "Could not resolve SPDX License Identifier of "
                            "{path}, resolving to {identifier}. Make sure the "
                            "license is in the license list found at "
                            "<https://spdx.org/licenses/> or that it starts "
                            "with 'LicenseRef-', and that it has a file "
                            "extension."
                        ).format(path=path, identifier=identifier)
                    )

            if identifier in license_files:
                _LOGGER.critical(
                    _(
                        "{identifier} is the SPDX License Identifier of both "
                        "{path} and {other_path}"
                    ).format(
                        identifier=identifier,
                        path=path,
                        other_path=license_files[identifier],
                    )
                )
                raise RuntimeError(
                    "Multiple licenses resolve to {}".format(identifier)
                )
            # Add the identifiers
            license_files[identifier] = path
            if (
                identifier.startswith("LicenseRef-")
                and "Unknown" not in identifier
            ):
                self.license_map[identifier] = {
                    "reference": str(path),
                    "isDeprecatedLicenseId": False,
                    "detailsUrl": None,
                    "referenceNumber": None,
                    "name": identifier,
                    "licenseId": identifier,
                    "seeAlso": [],
                    "isOsiApproved": None,
                }

        return license_files


def create_project() -> Project:
    """Create a project object. Try to find the project root from CWD,
    otherwise treat CWD as root.
    """
    root = find_root()
    if root is None:
        root = Path.cwd()
    return Project(root)

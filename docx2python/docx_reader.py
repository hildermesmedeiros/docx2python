#!/usr/bin/env python3
# _*_ coding: utf-8 _*_
""" Hold and decode docx internal xml files.

:author: Shay Hill
:created: 3/18/2021

See the docx file structure in ``README_DOCX_FILE_STRUCTURE.md``. Each file in that
structure can be stored as a ``File`` instance, though not all will be stored through
the typical docx2python progression.

The ``File`` class is designed to hold and decode xml files with content (text).
Several of the xml files in a docx do not contain content. These contain numbering
formats, font information, rId-lookup tables, and other. ``File`` instances will hold
these as well, though they will not have ``rels`` or ``content`` attributes. Will
return an empty dictionary or empty list if asked.

Some of these non-content files are shared between between . The substance of these
files is accessible through the ``DocxContent`` class. This class holds file
instances and decodes shared non-content in a docx file structure.
"""

from __future__ import annotations

import copy
import os
import pathlib
import zipfile
from contextlib import suppress
from dataclasses import dataclass
from operator import attrgetter
from typing import Dict, List, Optional, Set, Union
from warnings import warn
from pathlib import Path

from lxml import etree

from .attribute_register import XML2HTML_FORMATTER
from .docx_context import collect_numFmts, collect_rels
from .docx_text import get_text
from .merge_runs import merge_elems

CONTENT_FILE_TYPES = {"officeDocument", "header", "footer", "footnotes", "endnotes"}


@dataclass
class File:
    """The attribute dict of a file in the docx, plus cached data

    The docx lists internal files in various _rels files. Each will be specified with a
    dict of, e.g.::

        {
            'Id': 'rId8',
            'Type': 'http://schemas.openxmlformats.org/.../relationships/header',
            'Target': 'header1.xml'
        }

    This isn't quite enough to infer the structure of the docx. You'll also need to
    know the directory where each attribute dict was found::

        'dir': 'word/_rels'

    That's the starting point for these instances. Other attributes are inferred or
    created at runtime.
    """

    def __init__(self, context: DocxReader, attribute_dict: Dict[str, str]) -> None:
        """
        Point to container DocxContext instance and store attributes as properties.

        :param context: The DocxContent object holding this instance
        :param attribute_dict: Attributes of this file found in the rels, plus 'dir' as
        described above.
        """
        self.context = context
        self.Id = str(attribute_dict["Id"])
        self.Type = os.path.basename(attribute_dict["Type"])
        self.Target = attribute_dict["Target"]
        self.dir = attribute_dict["dir"]

        # old-style cached_property for Python 3.6 and 3.7 support
        self.__path: Union[None, str] = None
        self.__rels_path: Union[None, str] = None
        self.__rels: Union[None, Dict[str, str]] = None
        self.__root_element: Union[None, etree._Element] = None

    def __repr__(self) -> str:
        """File with self.path"""
        return f"File({self.path})"

    @property
    def path(self) -> str:
        """Infer path/to/xml/file from instance attributes

        :returns: path to xml file

        This will take the information in a file specification (from one of the rels
        files, e.g., {Id:'  ', Type:'  ' Target:'  ', dir:'  '}) and infer a path to
        the specified xml file.

        E.g.,
        from     self.dir = '_rels'       self.Target = 'word/document.xml
                    dirname ''          +       dirname 'word/'
                                        +       filename =   'document.xml'
        return `word/document`

        E.g.,
        from     self.dir = 'word/_rels'       self.Target = 'header1.xml
                    dirname 'word'      +            dirname ''
                                        +       filename =   'header1.xml'
        return `word/header1.xml`
        """
        if self.__path is not None:
            return self.__path

        dirs = [os.path.dirname(x) for x in (self.dir, self.Target)]
        dirname = "/".join([x for x in dirs if x])
        filename = os.path.basename(self.Target)
        self.__path = "/".join([dirname, filename])
        return self.__path

    @property
    def _rels_path(self) -> str:
        """Infer path/to/rels from instance attributes.

        :returns: path to rels (which may not exist)

        Every content file (``document.xml``, ``header1.xml``, ...) will have its own
        ``.rels`` file--if any relationships are defined.

        The path inferred here may not exist.

        E.g.,
        from     self.dir = '_rels'       self.Target = 'word/document.xml
                    dirname ''          +       dirname 'word/'
                                        +       filename =   'document.xml'
        return `word/_rels/document.xml.rels`

        E.g.,
        from     self.dir = 'word/_rels'       self.Target = 'header1.xml
                    dirname 'word'      +            dirname ''
                                        +       filename =   'header1.xml'
        return `word/_rels/header1.xml.rels`
        """
        if self.__rels_path is not None:
            return self.__rels_path
        dirname, filename = os.path.split(self.path)
        self.__rels_path = "/".join([dirname, "_rels", filename + ".rels"])
        return self.__rels_path

    @property
    def rels(self) -> Dict[str, str]:
        """rIds mapped to values

        Each content file.xml will have a file.xml.rels file--if relationships are
        defined. Inside file.xml, values defined in the file.xml.rels file may be
        referenced by their rId numbers.

        :returns: Contents of the file.xml.rels file with reference rId numbers. These
        refer to values defined in the file.xml.rels file:

        E.g.::

        {
            "rId3": "webSettings.xml",
            "rId2": "settings.xml",
            "rId1": "styles.xml",
            "rId6": "theme/theme1.xml",
            "rId5": "fontTable.xml",
            "rId4": "https://www.shayallenhill.com/",
        }

        Not every xml file with have a rels file. Return an empty dictionary if the
        rels file is not found.
        """
        if self.__rels is not None:
            return self.__rels

        try:
            unzipped = self.context.zipf.read(self._rels_path)
            tree = etree.fromstring(unzipped)
            self.__rels = {str(x.attrib["Id"]): str(x.attrib["Target"]) for x in tree}
        except KeyError:
            self.__rels = {}
        return self.__rels

    @property
    def root_element(self) -> etree._Element:
        """Root element of the file.

        Try to merge consecutive, duplicate (except text) elements in content files.
        See documentation for ``merge_elems``. Warn if ``merge_elems`` fails.
        (I don't think it will fail).
        """
        if self.__root_element is not None:
            return self.__root_element

        root = etree.fromstring(self.context.zipf.read(self.path))
        if self.Type in CONTENT_FILE_TYPES:
            root_ = copy.copy(root)
            try:
                merge_elems(self, root)
            except Exception as ex:
                warn(
                    f"Attempt to merge consecutive elements in "
                    f"{self.context.docx_filename} {self.path} resulted in "
                    f"{repr(ex)}. Moving on."
                )
                self.__root_element = root_
        self.__root_element = root
        return self.__root_element

    @property
    def content(self) -> List[List[List[List[str]]]]:
        """Text extracted into a 5-layer-deep nested list of strings."""
        return get_text(self)

    def get_content(
        self, root: Optional[etree._Element] = None
    ) -> List[List[List[List[str]]]]:
        """
        The same content as property 'content' with optional given root.

        :param root: Extract content of file from root down.
            If root is not given, return full content of file.
        """
        return get_text(self, root)


@dataclass
class DocxReader:
    """
    Hold File instances and decode information shared between them (e.g., numFmts)
    """

    def __init__(
        self,
        docx_filename: Union[Path, str],
        html: bool = False,
        paragraph_styles: bool = False,
    ):
        self.docx_filename = docx_filename
        self.do_pStyle = paragraph_styles

        if html:
            self.xml2html_format = XML2HTML_FORMATTER
        else:
            self.xml2html_format = {}

        # old-style cached_propery for Python 3.6 and 3.7 support
        self.__zipf: Union[None, zipfile.ZipFile] = None
        self.__files: Union[None, List[File]] = None
        self.__numId2NumFmts: Union[None, Dict[str, List[str]]] = None

    @property
    def zipf(self) -> zipfile.ZipFile:
        """
        Entire docx unzipped into bytes.
        """
        if self.__zipf is not None:
            return self.__zipf

        self.__zipf = zipfile.ZipFile(self.docx_filename)
        return self.__zipf

    @property
    def files(self) -> List[File]:
        """
        Instantiate a File instance for every content file.
        """
        if self.__files is not None:
            return self.__files

        files = []
        for k, v in collect_rels(self.zipf).items():
            files += [File(self, {**x, "dir": os.path.dirname(k)}) for x in v]
        self.__files = files
        return self.__files

    # noinspection PyPep8Naming
    @property
    def numId2numFmts(self) -> Dict[str, List[str]]:
        """
        numId referenced in xml to list of numFmt per indentation level

        See docstring for collect_numFmts

        Returns an empty dictionary if word/numbering.xml cannot be found.
        Ultimately, this will result in any lists (there should NOT be any lists if
        there is no word/numbering.xml) being "numbered" with "--".
        """
        if self.__numId2NumFmts is not None:
            return self.__numId2NumFmts

        try:
            numFmts_root = etree.fromstring(self.zipf.read("word/numbering.xml"))
            self.__numId2NumFmts = collect_numFmts(numFmts_root)
        except KeyError:
            self.__numId2NumFmts = {}
        return self.__numId2NumFmts

    def file_of_type(self, type_: str) -> File:
        """
        Return file instance attrib Type='http://.../type_'. Warn if more than one.

        :param type_: this package looks for any of
            ("header", "officeDocument", "footer", "footnotes", "endnotes")
            You can try others.
        :return: File instance of the requested type
        """
        files_of_type = self.files_of_type(type_)
        if len(files_of_type) > 1:
            warn("Multiple files of type '{type_}' found. Returning first.")
        try:
            return files_of_type[0]
        except IndexError:
            raise KeyError(
                f"There is no item of type '{type_}' "
                "in the {self.docx_filename} archive"
            )

    def files_of_type(self, type_: Optional[str] = None) -> List[File]:
        """
        File instances with attrib Type='http://.../type_'

        :param type_: this package looks for any of
            ("header", "officeDocument", "footer", "footnotes", "endnotes")
            You can try others. If argument is None (default), returns all content file
            types.
        :return: File instances of the requested type, sorted by path
        """
        if type_ is None:
            types = CONTENT_FILE_TYPES
        else:
            types = {type_}

        return sorted(
            (x for x in self.files if x.Type in types), key=attrgetter("path")
        )

    def content_files(self) -> List[File]:
        """
        Content files (contain displayed text) inside the docx.

        :return: File instances of context files, sorted by path
        """
        return self.files_of_type()

    def save(self, filename: Union[Path, str]) -> None:
        """
        Save the (presumably altered) xml.

        :param filename: path to output file (presumably *.docx)

        xml (root_element) attributes are cached, so these can be altered and saved.
        This allows saving a copy of the input docx after the ``merge_elems`` operation.
        Also allows some light editing like search and replace.
        """
        content_files = [x for x in self.files if x.Type in CONTENT_FILE_TYPES]
        with zipfile.ZipFile(f"{filename}", mode="w") as zout:
            _copy_but(self.zipf, zout, {x.path for x in content_files})
            for file in content_files:
                zout.writestr(file.path, etree.tostring(file.root_element))

    def pull_image_files(
        self, image_directory: Optional[str] = None
    ) -> Dict[str, bytes]:
        """
        Copy images from zip file.

        :param image_directory: optional destination for copied images
        :return: Image names mapped to images in binary format.

            To write these to disc::

                with open(key, 'wb') as file:
                    file.write(value)

        :side effects: Given an optional image_directory, will write the images out
        to file.
        """
        images = {}
        for image in self.files_of_type("image"):
            with suppress(KeyError):
                images[os.path.basename(image.Target)] = self.zipf.read(image.path)
        if image_directory is not None:
            pathlib.Path(image_directory).mkdir(parents=True, exist_ok=True)
            for file, image_bytes in images.items():
                with open(os.path.join(image_directory, file), "wb") as image_copy:
                    image_copy.write(image_bytes)
        return images

    def close(self):
        self.__zipf.close()

    def __del__(self):
        self.__zipf.close()

def _copy_but(
    in_zip: zipfile.ZipFile, out_zip: zipfile.ZipFile, exclusions: Optional[Set] = None
) -> None:
    """
    Copy every file in a docx except those listed in exclusions.

    :param in_zip: zipfile of origin xml file
    :param out_zip: zipfile of destination xml file
    :param exclusions: filenames you don't want to copy (e.g., 'document.xml')
    """
    exclusions = exclusions or set()
    for item in in_zip.infolist():
        if item.filename not in exclusions:
            buffer = in_zip.read(item.filename)
            out_zip.writestr(item, buffer)
    in_zip.close()
    out_zip.close()
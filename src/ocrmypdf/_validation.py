#!/usr/bin/env python3
# © 2015-17 James R. Barlow: github.com/jbarlow83
#
# This file is part of OCRmyPDF.
#
# OCRmyPDF is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OCRmyPDF is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with OCRmyPDF.  If not, see <http://www.gnu.org/licenses/>.


import logging
import os

import sys
import textwrap
from pathlib import Path

import PIL

from ._unicodefun import verify_python3_env

from .exec import (
    ghostscript,
    jbig2enc,
    qpdf,
    tesseract,
    check_external_program,
    unpaper,
    pngquant,
)
from .helpers import is_file_writable, re_symlink
from .exceptions import (
    BadArgsError,
    ExitCode,
    InputFileError,
    MissingDependencyError,
    OutputFileAccessError,
)

# -------------
# External dependencies

HOCR_OK_LANGS = frozenset(['eng', 'deu', 'spa', 'ita', 'por'])


def complain(message):
    print(*textwrap.wrap(message), file=sys.stderr)


# --------
# Critical environment tests
verify_python3_env()


def check_options_languages(options, _log):
    if not options.language:
        options.language = ['eng']  # Enforce English hegemony

    # Support v2.x "eng+deu" language syntax
    if '+' in options.language[0]:
        options.language = options.language[0].split('+')

    languages = set(options.language)
    if not languages.issubset(tesseract.languages()):
        msg = (
            "The installed version of tesseract does not have language "
            "data for the following requested languages: \n"
        )
        for lang in languages - tesseract.languages():
            msg += lang + '\n'
        raise MissingDependencyError(msg)


def check_options_output(options, log):
    # We have these constraints to check for.
    # 1. Ghostscript < 9.20 mangles multibyte Unicode
    # 2. hocr doesn't work on non-Latin languages (so don't select it)

    languages = set(options.language)
    is_latin = languages.issubset(HOCR_OK_LANGS)

    if options.pdf_renderer == 'hocr' and not is_latin:
        msg = (
            "The 'hocr' PDF renderer is known to cause problems with one "
            "or more of the languages in your document.  Use "
            "--pdf-renderer auto (the default) to avoid this issue."
        )
        log.warning(msg)

    if ghostscript.version() < '9.20' and options.output_type != 'pdf' and not is_latin:
        # https://bugs.ghostscript.com/show_bug.cgi?id=696874
        # Ghostscript < 9.20 fails to encode multibyte characters properly
        msg = (
            "The installed version of Ghostscript does not work correctly "
            "with the OCR languages you specified. Use --output-type pdf or "
            "upgrade to Ghostscript 9.20 or later to avoid this issue."
        )
        msg += f"Found Ghostscript {ghostscript.version()}"
        log.warning(msg)

    # Decide on what renderer to use
    if options.pdf_renderer == 'auto':
        options.pdf_renderer = 'sandwich'

    if options.output_type == 'pdfa':
        options.output_type = 'pdfa-2'

    if options.output_type == 'pdfa-3' and ghostscript.version() < '9.19':
        raise MissingDependencyError(
            "--output-type pdfa-3 requires Ghostscript 9.19 or later"
        )

    lossless_reconstruction = False
    if not any(
        (
            options.deskew,
            options.clean_final,
            options.force_ocr,
            options.remove_background,
        )
    ):
        lossless_reconstruction = True
    options.lossless_reconstruction = lossless_reconstruction

    if not options.lossless_reconstruction and options.redo_ocr:
        raise BadArgsError(
            "--redo-ocr is not currently compatible with --deskew, "
            "--clean-final, and --remove-background",
        )


def check_options_sidecar(options, log):
    if options.sidecar == '\0':
        if options.output_file == '-':
            raise BadArgsError(
                "--sidecar filename must be specified when output file is " "stdout.",
            )
        options.sidecar = options.output_file + '.txt'


def check_options_preprocessing(options, log):
    if options.clean_final:
        options.clean = True
    if options.unpaper_args and not options.clean:
        raise BadArgsError("--clean is required for --unpaper-args")
    if options.clean:
        check_external_program(
            log=log,
            program='unpaper',
            package='unpaper',
            version_checker=unpaper.version,
            need_version='6.1',
            required_for=['--clean, --clean-final'],
        )
        try:
            if options.unpaper_args:
                options.unpaper_args = unpaper.validate_custom_args(
                    options.unpaper_args
                )
        except Exception as e:
            raise BadArgsError(str(e))


def check_options_ocr_behavior(options, log):
    exclusive_options = sum(
        [
            (1 if opt else 0)
            for opt in (options.force_ocr, options.skip_text, options.redo_ocr)
        ]
    )
    if exclusive_options >= 2:
        raise BadArgsError(
            "Error: choose only one of --force-ocr, --skip-text, --redo-ocr."
        )


def check_options_optimizing(options, log):
    if options.optimize >= 2:
        check_external_program(
            log=log,
            program='pngquant',
            package='pngquant',
            version_checker=pngquant.version,
            need_version='2.0.1',
            required_for='--optimize {2,3}',
        )

    if options.optimize >= 2:
        # Although we use JBIG2 for optimize=1, don't nag about it unless the
        # user is asking for more optimization
        check_external_program(
            log=log,
            program='jbig2',
            package='jbig2enc',
            version_checker=jbig2enc.version,
            need_version='0.28',
            required_for='--optimize {2,3} | --jbig2-lossy',
            recommended=True if not options.jbig2_lossy else False,
        )

    if options.optimize == 0 and any(
        [options.jbig2_lossy, options.png_quality, options.jpeg_quality]
    ):
        log.warning(
            "The arguments --jbig2-lossy, --png-quality, and --jpeg-quality "
            "will be ignored because --optimize=0."
        )


def check_options_advanced(options, log):
    if options.pdfa_image_compression != 'auto' and options.output_type.startswith(
        'pdfa'
    ):
        log.warning(
            "--pdfa-image-compression argument has no effect when "
            "--output-type is not 'pdfa', 'pdfa-1', or 'pdfa-2'"
        )
    if tesseract.v4() and (options.user_words or options.user_patterns):
        log.warning('Tesseract 4.x ignores --user-words, so this has no effect')


def check_options_metadata(options, log):
    import unicodedata

    docinfo = [options.title, options.author, options.keywords, options.subject]
    for s in (m for m in docinfo if m):
        for c in s:
            if unicodedata.category(c) == 'Co' or ord(c) >= 0x10000:
                raise ValueError(
                    "One of the metadata strings contains "
                    "an unsupported Unicode character: '{}' (U+{})".format(
                        c, hex(ord(c))[2:].upper()
                    )
                )


def check_options_pillow(options, log):
    PIL.Image.MAX_IMAGE_PIXELS = int(options.max_image_mpixels * 1_000_000)
    if PIL.Image.MAX_IMAGE_PIXELS == 0:
        PIL.Image.MAX_IMAGE_PIXELS = None


def check_options(options, log):
    try:
        check_options_languages(options, log)
        check_options_metadata(options, log)
        check_options_output(options, log)
        check_options_sidecar(options, log)
        check_options_preprocessing(options, log)
        check_options_ocr_behavior(options, log)
        check_options_optimizing(options, log)
        check_options_advanced(options, log)
        check_options_pillow(options, log)
        return ExitCode.ok
    except ValueError as e:
        log.error(e)
        return ExitCode.bad_args
    except BadArgsError as e:
        log.error(e)
        return e.exit_code
    except MissingDependencyError as e:
        log.error(e)
        return ExitCode.missing_dependency


# ----------
# Logging


def logging_factory(logger_name, logger_args):
    verbose = logger_args['verbose']
    quiet = logger_args['quiet']

    root_logger = logging.getLogger(logger_name)
    root_logger.setLevel(logging.DEBUG)

    handler = logging.StreamHandler(sys.stderr)
    formatter_ = logging.Formatter("%(levelname)7s - %(message)s")
    handler.setFormatter(formatter_)
    if verbose:
        handler.setLevel(logging.DEBUG)
    elif quiet:
        handler.setLevel(logging.WARNING)
    else:
        handler.setLevel(logging.INFO)
    root_logger.addHandler(handler)
    return root_logger


def check_closed_streams(options):
    """Work around Python issue with multiprocessing forking on closed streams

    https://bugs.python.org/issue28326

    Attempting to a fork/exec a new Python process when any of std{in,out,err}
    are closed or not flushable for some reason may raise an exception.
    Fix this by opening devnull if the handle seems to be closed.  Do this
    globally to avoid tracking places all places that fork.

    Seems to be specific to multiprocessing.Process not all Python process
    forkers.

    The error actually occurs when the stream object is not flushable,
    but replacing an open stream object that is not flushable with
    /dev/null is a bad idea since it will create a silent failure.  Replacing
    a closed handle with /dev/null seems safe.

    """

    if sys.version_info[0:3] >= (3, 6, 4):
        return True  # Issued fixed in Python 3.6.4+

    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w')

    if sys.stdin is None:
        if options.input_file == '-':
            print("Trying to read from stdin but stdin seems closed", file=sys.stderr)
            return False
        sys.stdin = open(os.devnull, 'r')

    if sys.stdout is None:
        if options.output_file == '-':
            # Can't replace stdout if the user is piping
            # If this case can even happen, it must be some kind of weird
            # stream.
            print(
                textwrap.dedent(
                    """\
                Output was set to stdout '-' but the stream attached to
                stdout does not support the flush() system call.  This
                will fail."""
                ),
                file=sys.stderr,
            )
            return False
        sys.stdout = open(os.devnull, 'w')

    return True


def log_page_orientations(pdfinfo, _log):
    direction = {0: 'n', 90: 'e', 180: 's', 270: 'w'}
    orientations = []
    for n, page in enumerate(pdfinfo):
        angle = page.rotation or 0
        if angle != 0:
            orientations.append('{0}{1}'.format(n + 1, direction.get(angle, '')))
    if orientations:
        _log.info('Page orientations detected: ' + ' '.join(orientations))


def check_environ(options, _log):
    old_envvars = (
        'OCRMYPDF_TESSERACT',
        'OCRMYPDF_QPDF',
        'OCRMYPDF_GS',
        'OCRMYPDF_UNPAPER',
    )
    for k in old_envvars:
        if k in os.environ:
            _log.warning(
                textwrap.dedent(
                    f"""\
                OCRmyPDF no longer uses the environment variable {k}.
                Change PATH to select alternate programs."""
                )
            )


def create_input_file(options, log, work_folder):
    if options.input_file == '-':
        # stdin
        log.info('reading file from standard input')
        target = os.path.join(work_folder, 'stdin')
        with open(target, 'wb') as stream_buffer:
            from shutil import copyfileobj
            copyfileobj(sys.stdin.buffer, stream_buffer)
        return target
    else:
        try:
            target = os.path.join(work_folder, 'origin')
            re_symlink(options.input_file, target, log)
            return target
        except FileNotFoundError:
            log.error("File not found - " + options.input_file)
            raise InputFileError()


def check_input_file(options, _log, start_input_file):
    if options.input_file == '-':
        # stdin
        _log.info('reading file from standard input')
        with open(start_input_file, 'wb') as stream_buffer:
            from shutil import copyfileobj

            copyfileobj(sys.stdin.buffer, stream_buffer)
    else:
        try:
            re_symlink(options.input_file, start_input_file, _log)
        except FileNotFoundError:
            _log.error("File not found - " + options.input_file)
            raise InputFileError()


def check_requested_output_file(options, _log):
    if options.output_file == '-':
        if sys.stdout.isatty():
            _log.error(
                textwrap.dedent(
                    """\
                Output was set to stdout '-' but it looks like stdout
                is connected to a terminal.  Please redirect stdout to a
                file."""
                )
            )
            raise BadArgsError()
    elif not is_file_writable(options.output_file):
        _log.error(
            "Output file location (" + options.output_file + ") is not a writable file."
        )
        raise OutputFileAccessError()


def report_output_file_size(options, _log, input_file, output_file):
    try:
        output_size = Path(output_file).stat().st_size
        input_size = Path(input_file).stat().st_size
    except FileNotFoundError:
        return  # Outputting to stream or something
    ratio = output_size / input_size
    if ratio < 1.35 or input_size < 25000:
        return  # Seems fine

    reasons = []
    image_preproc = {
        'deskew',
        'clean_final',
        'remove_background',
        'oversample',
        'force_ocr',
    }
    for arg in image_preproc:
        attr = getattr(options, arg, None)
        if not attr:
            continue
        reasons.append(
            f"The argument --{arg.replace('_', '-')} was issued, causing transcoding."
        )

    if reasons:
        explanation = "Possible reasons for this include:\n" + '\n'.join(reasons) + "\n"
    else:
        explanation = "No reason for this increase is known.  Please report this issue."

    _log.warning(
        textwrap.dedent(
            f"""\
        The output file size is {ratio:.2f}× larger than the input file.
        {explanation}
        """
        )
    )


def check_dependency_versions(options, log):
    check_external_program(
        log=log,
        program='tesseract',
        package={'darwin': 'tesseract', 'linux': 'tesseract-ocr'},
        version_checker=tesseract.version,
        need_version='4.0.0',  # using backport for Travis CI
    )
    check_external_program(
        log=log,
        program='gs',
        package='ghostscript',
        version_checker=ghostscript.version,
        need_version='9.15',  # limited by Travis CI / Ubuntu 14.04 backports
    )
    if ghostscript.version() == '9.24':
        complain(
            "Ghostscript 9.24 contains serious regressions and is not "
            "supported. Please upgrade to Ghostscript 9.25 or use an older "
            "version."
        )
        return ExitCode.missing_dependency
    check_external_program(
        log=log,
        program='qpdf',
        package='qpdf',
        version_checker=qpdf.version,
        need_version='8.0.2',
    )

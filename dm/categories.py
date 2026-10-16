"""File type categories (by extension). Colors live in the CSS (--cat-<key>)."""

CATEGORIES = [
    # key, French label — 8 colored categories (fixed palette order) + "other"
    ("app", "Applications & système"),
    ("data", "Données & caches"),
    ("disk_image", "Images disque & VM"),
    ("video", "Vidéo & audio"),
    ("image", "Images"),
    ("document", "Documents"),
    ("code", "Code & projets"),
    ("archive", "Archives"),
    ("other", "Autres"),
]

_EXT = {
    "video": "mp4 mkv avi mov wmv flv webm m4v mpg mpeg 3gp ts mts m2ts vob ogv "
             "mp3 wav flac aac ogg m4a wma opus aiff alac mid midi",
    "image": "jpg jpeg png gif bmp tif tiff webp heic heif raw cr2 cr3 nef arw dng psd ai svg ico xcf",
    "document": "pdf doc docx xls xlsx ppt pptx odt ods odp rtf txt md csv epub tex pages key numbers "
                "one vsdx xps",
    "archive": "zip rar 7z tar gz tgz bz2 xz zst lz lzma cab jar",
    "disk_image": "iso img vhd vhdx vmdk vdi qcow2 wim esd ppkg dmg swm",
    "app": "exe dll sys msi msp msix msixbundle appx appxbundle ocx mui pak asar node pyd so lib drv "
           "efi cpl scr com bat cmd ps1 cat manifest",
    "code": "py pyc ipynb js mjs cjs ts tsx jsx json html htm css scss c cc cpp cxx h hpp cs java kt go rs "
            "rb php swift r m sql yml yaml toml ini cfg xml lock sh vue svelte ipch pch obj o",
    "data": "db sqlite sqlite3 dat log tmp temp cache ldb idx bin blob pst ost etl evtx bak old dmp "
            "mdmp hdmp chk qpg pf jrs edb",
}

EXT_TO_CAT = {ext: cat for cat, exts in _EXT.items() for ext in exts.split()}
# Extension-less files are overwhelmingly caches / app data (browser caches,
# git objects, package-manager stores).
EXT_TO_CAT[""] = "data"
CATEGORY_KEYS = [k for k, _ in CATEGORIES]


def category_of(name: str) -> str:
    return EXT_TO_CAT.get(ext_of(name), "other")


def ext_of(name: str) -> str:
    dot = name.rfind(".")
    if dot <= 0 or dot == len(name) - 1:
        return ""
    ext = name[dot + 1:].lower()
    return ext if len(ext) <= 12 else ""

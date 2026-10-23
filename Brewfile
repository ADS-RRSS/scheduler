# Lo que Scheduler necesita en un Mac:  brew bundle   (o  make bootstrap)

# La app pide Python 3.11 o superior; el python3 que trae macOS es el 3.9 de Xcode.
brew "python@3.13"
# ffprobe valida cada vídeo (duración, códec, audio…) antes de subirlo.
brew "ffmpeg"
# Opcional: un modelo local escribe las descripciones. Después:
#   brew services start ollama  &&  make ollama
brew "ollama"

# Sólo si vas a usarlo en Docker (make docker-up). OrbStack es ligero en Apple
# Silicon; Docker Desktop también sirve. Revisa la licencia si es para una empresa.
# cask "orbstack"

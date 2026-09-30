"""Small, optional Discord avatar snapshots for local Wordle PNGs."""
import asyncio
import hashlib
import io
import re
import tempfile
import time
from collections import OrderedDict
from pathlib import Path


def avatar_key(value):
    return isinstance(value,str) and re.fullmatch(r'[0-9a-f]{64}',value) is not None


def normalize_avatar(data):
    from PIL import Image,ImageOps
    if len(data)>1_000_000:
        raise ValueError('Avatar is too large')
    with Image.open(io.BytesIO(data)) as source:
        if source.width>512 or source.height>512:
            raise ValueError('Avatar dimensions are too large')
        image=ImageOps.fit(source.convert('RGBA'),(96,96),method=Image.Resampling.LANCZOS)
    output=io.BytesIO()
    image.save(output,format='PNG',optimize=True)
    return output.getvalue()


class AvatarCache:
    def __init__(self,folder=None):
        self.folder=Path(folder) if folder else None
        self.memory=OrderedDict()
        self.failed=OrderedDict()
        self.pending={}
        self.slots=asyncio.Semaphore(4)

    def _read(self,key):
        if not avatar_key(key):
            return None
        data=self.memory.get(key)
        if data is not None:
            return data
        if self.folder:
            path=self.folder/(key+'.png')
            try:
                if path.stat().st_size<=100_000:
                    return normalize_avatar(path.read_bytes())
            except Exception:
                pass
        return None

    def _write(self,key,data):
        if not self.folder:
            return
        temporary=None
        try:
            self.folder.mkdir(parents=True,exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.folder,suffix='.tmp',delete=False) as stream:
                temporary=Path(stream.name)
                stream.write(data)
            temporary.replace(self.folder/(key+'.png'))
        except OSError:
            # An unwritable disk must not prevent an in-memory avatar or the game.
            pass
        finally:
            if temporary:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    async def _capture(self,key,asset):
        try:
            async with asyncio.timeout(3):
                data=await asyncio.to_thread(self._read,key)
                if data is None:
                    async with self.slots:
                        raw=await asset.read()
                        data=await asyncio.to_thread(normalize_avatar,raw)
                        await asyncio.to_thread(self._write,key,data)
                self.memory[key]=data
                self.memory.move_to_end(key)
                while len(self.memory)>128:
                    self.memory.popitem(last=False)
                return key
        except Exception:
            self.failed[key]=time.monotonic()+300
            self.failed.move_to_end(key)
            while len(self.failed)>256:
                self.failed.popitem(last=False)
            return None

    async def snapshot(self,member):
        """Only SDK-owned assets are used; never accept a user-supplied URL."""
        try:
            asset=member.display_avatar.replace(size=64,format='png',static_format='png')
            key=hashlib.sha256(asset.url.encode()).hexdigest()
        except (AttributeError,TypeError,ValueError):
            return None
        if key in self.memory:
            self.memory.move_to_end(key)
            return key
        if self.failed.get(key,0)>time.monotonic():
            return None
        task=self.pending.get(key)
        if task is None:
            task=asyncio.create_task(self._capture(key,asset))
            self.pending[key]=task
            task.add_done_callback(lambda done:self.pending.pop(key,None))
        return await asyncio.shield(task)

    def images(self,game):
        """Load saved snapshots on a rendering worker, without network access."""
        result={}
        for key in {guess.get('avatar') for guess in game['guesses']}:
            data=self._read(key)
            if data:
                result[key]=data
        return result

    def prune(self,keep):
        if not self.folder or not self.folder.is_dir():
            return
        cutoff=time.time()-30*86400
        for path in self.folder.glob('*.png'):
            if avatar_key(path.stem) and path.stem not in keep:
                try:
                    if path.stat().st_mtime<cutoff:
                        path.unlink()
                        self.memory.pop(path.stem,None)
                except OSError:
                    pass

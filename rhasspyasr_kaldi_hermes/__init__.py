"""Hermes MQTT server for Rhasspy ASR using Kaldi"""
import gzip
import zipfile
import hashlib
import logging
import os
import threading
import typing
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue

import networkx as nx
import rhasspyasr_kaldi
import rhasspynlu
from rhasspyasr import Transcriber, Transcription
from rhasspyasr_kaldi.train import LanguageModelType
from rhasspyhermes.asr import (
    AsrAudioCaptured,
    AsrError,
    AsrRecordingFinished,
    AsrStartListening,
    AsrStopListening,
    AsrTextCaptured,
    AsrToggleOff,
    AsrToggleOn,
    AsrToggleReason,
    AsrTrain,
    AsrTrainSuccess,
)
from rhasspyhermes.audioserver import AudioFrame, AudioSessionFrame
from rhasspyhermes.base import Message
from rhasspyhermes.client import GeneratorType, HermesClient, TopicArgs
from rhasspyhermes.g2p import G2pError, G2pPhonemes, G2pPronounce, G2pPronunciation
from rhasspyhermes.nlu import AsrToken, AsrTokenTime
from rhasspynlu.g2p import PronunciationsType
from rhasspysilence import SilenceMethod, VoiceCommandRecorder, WebRtcVadRecorder

from . import utils

_LOGGER = logging.getLogger("rhasspyasr_kaldi_hermes")

# -----------------------------------------------------------------------------

AudioCapturedType = typing.Tuple[AsrAudioCaptured, TopicArgs]
StopListeningType = typing.Union[
    AsrRecordingFinished, AsrTextCaptured, AsrError, AudioCapturedType
]


@dataclass
class TranscriberInfo:
    """Objects for a single transcriber"""

    transcriber: typing.Optional[Transcriber] = None
    recorder: typing.Optional[VoiceCommandRecorder] = None
    frame_queue: "Queue[typing.Optional[bytes]]" = field(default_factory=Queue)
    ready_event: threading.Event = field(default_factory=threading.Event)
    result: typing.Optional[Transcription] = None
    result_event: threading.Event = field(default_factory=threading.Event)
    result_sent: bool = False
    start_listening: typing.Optional[AsrStartListening] = None
    thread: typing.Optional[threading.Thread] = None
    audio_buffer: typing.Optional[bytes] = None
    reuse: bool = True


@dataclass
class PronunciationDictionary:
    """Details of a phonetic dictionary."""

    path: Path
    pronunciations: PronunciationsType = field(default_factory=dict)
    mtime_ns: typing.Optional[int] = None


# -----------------------------------------------------------------------------


class AsrHermesMqtt(HermesClient):
    """Hermes MQTT server for Rhasspy ASR using Kaldi."""

    def __init__(
        self,
        client,
        transcriber_factory: typing.Callable[[], Transcriber],
        model_dir: typing.Optional[Path] = None,
        graph_dir: typing.Optional[Path] = None,
        base_dictionaries: typing.Optional[typing.List[Path]] = None,
        dictionary_word_transform: typing.Optional[typing.Callable[[str], str]] = None,
        g2p_model: typing.Optional[Path] = None,
        g2p_word_transform: typing.Optional[typing.Callable[[str], str]] = None,
        dictionary_path: typing.Optional[Path] = None,
        language_model_path: typing.Optional[Path] = None,
        language_model_type: LanguageModelType = LanguageModelType.ARPA,
        unknown_words: typing.Optional[Path] = None,
        no_overwrite_train: bool = False,
        base_language_model_fst: typing.Optional[Path] = None,
        base_language_model_weight: float = 0,
        mixed_language_model_fst: typing.Optional[Path] = None,
        site_ids: typing.Optional[typing.List[str]] = None,
        enabled: bool = True,
        sample_rate: int = 16000,
        sample_width: int = 2,
        channels: int = 1,
        recorder_factory: typing.Optional[
            typing.Callable[[], VoiceCommandRecorder]
        ] = None,
        skip_seconds: float = 0.0,
        min_seconds: float = 1.0,
        max_seconds: typing.Optional[float] = None,
        speech_seconds: float = 0.3,
        silence_seconds: float = 0.5,
        before_seconds: float = 0.5,
        vad_mode: int = 3,
        max_energy: typing.Optional[float] = None,
        max_current_energy_ratio_threshold: typing.Optional[float] = None,
        current_energy_threshold: typing.Optional[float] = None,
        silence_method: SilenceMethod = SilenceMethod.VAD_ONLY,
        reuse_transcribers: bool = False,
        spn_phone: str = "SPN",
        sil_phone: str = "SIL",
        allow_unknown_words: bool = False,
        frequent_words: typing.Optional[typing.Set[str]] = None,
        unknown_words_probability: float = 1e-10,
        unknown_token: str = "<unk>",
        max_unknown_words: int = 8,
        silence_probability: float = 0.5,
        cancel_word: typing.Optional[str] = None,
        cancel_probability: float = 1e-2,
        lang: typing.Optional[str] = None,
    ):
        super().__init__(
            "rhasspyasr_kaldi_hermes",
            client,
            site_ids=site_ids,
            sample_rate=sample_rate,
            sample_width=sample_width,
            channels=channels,
        )

        self.subscribe(
            AsrToggleOn,
            AsrToggleOff,
            AsrStartListening,
            AsrStopListening,
            G2pPronounce,
            AudioFrame,
            AudioSessionFrame,
            AsrTrain,
        )

        self.transcriber_factory = transcriber_factory

        # Kaldi model/graph dirs
        self.model_dir = model_dir
        self.graph_dir = graph_dir

        # True if transcribers should be reused
        self.reuse_transcribers = reuse_transcribers
        self.kaldi_port: typing.Optional[int] = None

        if not self.reuse_transcribers:
            # Use a fixed port number
            self.kaldi_port = utils.get_free_port()

        # Files to write during training
        self.dictionary_path = dictionary_path
        self.language_model_path = language_model_path
        self.language_model_type = language_model_type

        # Pronunciation dictionaries and word transform function
        base_dictionaries = base_dictionaries or []
        self.base_dictionaries = [
            PronunciationDictionary(path=path) for path in base_dictionaries
        ]
        self.dictionary_word_transform = dictionary_word_transform

        # Grapheme-to-phonme model (Phonetisaurus FST) and word transform
        # function.
        self.g2p_model = g2p_model
        self.g2p_word_transform = g2p_word_transform

        # Mixed language model
        self.base_language_model_fst = base_language_model_fst
        self.base_language_model_weight = base_language_model_weight
        self.mixed_language_model_fst = mixed_language_model_fst

        # If True, HCLG.fst won't be overwritten during training
        self.no_overwrite_train = no_overwrite_train

        # Path to write missing words and guessed pronunciations
        self.unknown_words = unknown_words

        # Phone used for spoken noise (<unk>)
        self.spn_phone = spn_phone

        # Phone used for silence (<sil>)
        self.sil_phone = sil_phone

        # Used to produce alternative unknown word paths
        self.frequent_words = frequent_words
        self.allow_unknown_words = allow_unknown_words
        self.unknown_words_probability = unknown_words_probability
        self.unknown_token = unknown_token
        self.max_unknown_words = max_unknown_words
        self.silence_probability = silence_probability
        self.cancel_word = cancel_word
        self.cancel_probability = cancel_probability

        # True if ASR system is enabled
        self.enabled = enabled
        self.disabled_reasons: typing.Set[str] = set()

        # Seconds to wait for a result from a finished transcriber thread
        self.session_result_timeout = 20

        # Required audio format
        self.sample_rate = sample_rate
        self.sample_width = sample_width
        self.channels = channels

        # No timeout on silence detection
        def make_webrtcvad():
            return WebRtcVadRecorder(
                max_seconds=max_seconds,
                vad_mode=vad_mode,
                skip_seconds=skip_seconds,
                min_seconds=min_seconds,
                speech_seconds=speech_seconds,
                silence_seconds=silence_seconds,
                before_seconds=before_seconds,
                silence_method=silence_method,
                current_energy_threshold=current_energy_threshold,
                max_energy=max_energy,
                max_current_ratio_threshold=max_current_energy_ratio_threshold,
            )

        self.recorder_factory = recorder_factory or make_webrtcvad

        # WAV buffers for each session
        self.sessions: typing.Dict[typing.Optional[str], TranscriberInfo] = {}
        self.free_transcribers: typing.List[TranscriberInfo] = []

        self.first_audio: bool = True

        self.lang = lang

    # -------------------------------------------------------------------------

    async def start_listening(
        self, message: AsrStartListening
    ) -> typing.AsyncIterable[typing.Union[StopListeningType, AsrError]]:
        """Start recording audio data for a session."""
        try:
            if message.session_id in self.sessions:
                # Stop existing session
                async for stop_message in self.stop_listening(
                    AsrStopListening(
                        site_id=message.site_id, session_id=message.session_id
                    )
                ):
                    yield stop_message

            if self.free_transcribers:
                # Re-use existing transcriber
                info = self.free_transcribers.pop()

                _LOGGER.debug(
                    "Re-using existing transcriber (session_id=%s)", message.session_id
                )
            else:
                # Create new transcriber
                info = TranscriberInfo(reuse=self.reuse_transcribers)
                _LOGGER.debug("Creating new transcriber session %s", message.session_id)

                def transcribe_proc(
                    info, transcriber_factory, sample_rate, sample_width, channels
                ):
                    def audio_stream(frame_queue) -> typing.Iterable[bytes]:
                        # Pull frames from the queue
                        frames = frame_queue.get()
                        while frames:
                            yield frames
                            frames = frame_queue.get()

                    try:
                        info.transcriber = transcriber_factory(port_num=self.kaldi_port)

                        assert (
                            info.transcriber is not None
                        ), "Failed to create transcriber"

                        while True:
                            # Wait for session to start
                            info.ready_event.wait()
                            info.ready_event.clear()

                            # Get result of transcription
                            result = info.transcriber.transcribe_stream(
                                audio_stream(info.frame_queue),
                                sample_rate,
                                sample_width,
                                channels,
                            )

                            _LOGGER.debug("Transcription result: %s", result)

                            assert (
                                result is not None and result.text
                            ), "Null transcription"

                            # Signal completion
                            info.result = result
                            info.result_event.set()

                            if not self.reuse_transcribers:
                                try:
                                    info.transcriber.stop()
                                except Exception:
                                    _LOGGER.exception("Transcriber stop")

                                break
                    except Exception:
                        _LOGGER.exception("session proc")

                        # Mark as not reusable
                        info.reuse = False

                        # Stop transcriber
                        if info.transcriber is not None:
                            try:
                                info.transcriber.stop()
                            except Exception:
                                _LOGGER.exception("Transcriber stop")

                        # Signal failure
                        info.transcriber = None
                        info.result = Transcription(
                            text="", likelihood=0, transcribe_seconds=0, wav_seconds=0
                        )
                        info.result_event.set()

                # Run in separate thread
                info.thread = threading.Thread(
                    target=transcribe_proc,
                    args=(
                        info,
                        self.transcriber_factory,
                        self.sample_rate,
                        self.sample_width,
                        self.channels,
                    ),
                    daemon=True,
                )

                info.thread.start()

            # ---------------------------------------------------------------------

            # Settings for session
            info.start_listening = message

            # Signal session thread to start
            info.ready_event.set()

            if message.stop_on_silence:
                # Begin silence detection
                if info.recorder is None:
                    info.recorder = self.recorder_factory()

                info.recorder.start()
            else:
                # Use internal buffer (no silence detection)
                info.audio_buffer = bytes()

            self.sessions[message.session_id] = info
            _LOGGER.debug("Starting listening (session_id=%s)", message.session_id)
            self.first_audio = True
        except Exception as e:
            _LOGGER.exception("start_listening")
            yield AsrError(
                error=str(e),
                context=repr(message),
                site_id=message.site_id,
                session_id=message.session_id,
            )

    async def stop_listening(
        self, message: AsrStopListening
    ) -> typing.AsyncIterable[StopListeningType]:
        """Stop recording audio data for a session."""
        info = self.sessions.pop(message.session_id, None)
        if info:
            try:
                # Trigger publishing of transcription on end of session
                async for result in self.finish_session(
                    info, message.site_id, message.session_id
                ):
                    yield result

                if info.reuse and (info.transcriber is not None):
                    # Reset state
                    info.result = None
                    info.result_event.clear()
                    info.result_sent = False
                    info.result = None
                    info.start_listening = None
                    info.audio_buffer = None

                    while info.frame_queue.qsize() > 0:
                        info.frame_queue.get_nowait()

                    # Add to free pool
                    self.free_transcribers.append(info)
            except Exception as e:
                _LOGGER.exception("stop_listening")
                yield AsrError(
                    error=str(e),
                    context=repr(info.transcriber),
                    site_id=message.site_id,
                    session_id=message.session_id,
                )

        _LOGGER.debug("Stopping listening (session_id=%s)", message.session_id)

    async def handle_audio_frame(
        self,
        frame_wav_bytes: bytes,
        site_id: str = "default",
        session_id: typing.Optional[str] = None,
    ) -> typing.AsyncIterable[
        typing.Union[
            AsrRecordingFinished,
            AsrTextCaptured,
            AsrError,
            typing.Tuple[AsrAudioCaptured, TopicArgs],
        ]
    ]:
        """Process single frame of WAV audio"""

        # Don't process audio if no sessions
        if not self.sessions:
            return

        audio_data = self.maybe_convert_wav(frame_wav_bytes)

        if session_id is None:
            # Add to every open session
            target_sessions = list(self.sessions.items())
        else:
            # Add to single session
            target_sessions = [(session_id, self.sessions[session_id])]

        # Add to every open session with matching site_id
        for target_id, info in target_sessions:
            try:
                assert info.start_listening is not None

                # Match site_id
                if info.start_listening.site_id != site_id:
                    continue

                # Push to transcription thread
                info.frame_queue.put(audio_data)

                if info.recorder is not None:
                    # Check for voice command end
                    command = info.recorder.process_chunk(audio_data)

                    if command:
                        # Trigger publishing of transcription on silence
                        async for result in self.finish_session(
                            info, site_id=site_id, session_id=target_id
                        ):
                            yield result
                else:
                    # Use session audio buffer
                    assert info.audio_buffer is not None
                    info.audio_buffer += audio_data
            except Exception as e:
                _LOGGER.exception("handle_audio_frame")
                yield AsrError(
                    error=str(e),
                    context=repr(info.transcriber),
                    site_id=site_id,
                    session_id=target_id,
                )

    async def finish_session(
        self, info: TranscriberInfo, site_id: str, session_id: typing.Optional[str]
    ) -> typing.AsyncIterable[
        typing.Union[AsrRecordingFinished, AsrTextCaptured, AudioCapturedType]
    ]:
        """Publish transcription result for a session if not already published"""

        if info.recorder is not None:
            # Stop silence detection and get trimmed audio
            audio_data = info.recorder.stop()
        else:
            # Use complete audio buffer
            assert info.audio_buffer is not None
            audio_data = info.audio_buffer

        if not info.result_sent:
            # Send recording finished message
            yield AsrRecordingFinished(site_id=site_id, session_id=session_id)

            # Avoid re-sending transcription
            info.result_sent = True

            # Last chunk
            info.frame_queue.put(None)

            # Wait for result
            result_success = info.result_event.wait(timeout=self.session_result_timeout)
            if not result_success:
                # Mark transcription as non-reusable
                info.reuse = False

            transcription = info.result
            assert info.start_listening is not None

            if transcription:
                # Successful transcription
                asr_tokens: typing.Optional[typing.List[typing.List[AsrToken]]] = None

                if transcription.tokens:
                    # Only one level of ASR tokens
                    asr_inner_tokens: typing.List[AsrToken] = []
                    asr_tokens = [asr_inner_tokens]
                    range_start = 0
                    for ps_token in transcription.tokens:
                        range_end = range_start + len(ps_token.token) + 1
                        asr_inner_tokens.append(
                            AsrToken(
                                value=ps_token.token,
                                confidence=ps_token.likelihood,
                                range_start=range_start,
                                range_end=range_start + len(ps_token.token) + 1,
                                time=AsrTokenTime(
                                    start=ps_token.start_time, end=ps_token.end_time
                                ),
                            )
                        )

                        range_start = range_end

                yield (
                    AsrTextCaptured(
                        text=transcription.text,
                        likelihood=transcription.likelihood,
                        seconds=transcription.transcribe_seconds,
                        site_id=site_id,
                        session_id=session_id,
                        asr_tokens=asr_tokens,
                        lang=(info.start_listening.lang or self.lang),
                    )
                )
            else:
                # Empty transcription
                yield AsrTextCaptured(
                    text="",
                    likelihood=0,
                    seconds=0,
                    site_id=site_id,
                    session_id=session_id,
                    lang=(info.start_listening.lang or self.lang),
                )

            if info.start_listening.send_audio_captured:
                wav_bytes = self.to_wav_bytes(audio_data)

                # Send audio data
                yield (
                    # pylint: disable=E1121
                    AsrAudioCaptured(wav_bytes),
                    {"site_id": site_id, "session_id": session_id},
                )

    # -------------------------------------------------------------------------

    async def handle_train(
        self, train: AsrTrain, site_id: str = "default"
    ) -> typing.AsyncIterable[
        typing.Union[typing.Tuple[AsrTrainSuccess, TopicArgs], AsrError]
    ]:
        """Re-trains ASR system."""
        try:
            assert (
                self.model_dir and self.graph_dir
            ), "Model and graph dirs are required to train"

            # Load base dictionaries
            pronunciations: PronunciationsType = defaultdict(list)
            for base_dict in self.base_dictionaries:
                if not os.path.exists(base_dict.path):
                    _LOGGER.warning(
                        "Base dictionary does not exist: %s", base_dict.path
                    )
                    continue

                # Re-load dictionary if modification time has changed
                dict_mtime_ns = os.stat(base_dict.path).st_mtime_ns
                if (base_dict.mtime_ns is None) or (
                    base_dict.mtime_ns != dict_mtime_ns
                ):
                    base_dict.mtime_ns = dict_mtime_ns
                    _LOGGER.debug("Loading base dictionary from %s", base_dict.path)
                    with open(base_dict.path, "r") as base_dict_file:
                        rhasspynlu.g2p.read_pronunciations(
                            base_dict_file, word_dict=base_dict.pronunciations
                        )

                for word in base_dict.pronunciations:
                    pronunciations[word].extend(base_dict.pronunciations[word])

            if not self.no_overwrite_train:
                _LOGGER.debug("Loading %s", train.graph_path)
                with gzip.GzipFile(train.graph_path, mode="rb") as graph_gzip:
                    graph = nx.readwrite.gpickle.read_gpickle(graph_gzip)

                # Re-generate HCLG.fst
                _LOGGER.debug("Starting training")
                rhasspyasr_kaldi.train(
                    graph,
                    pronunciations,
                    self.model_dir,
                    self.graph_dir,
                    dictionary=self.dictionary_path,
                    language_model=self.language_model_path,
                    language_model_type=self.language_model_type,
                    dictionary_word_transform=self.dictionary_word_transform,
                    g2p_model=self.g2p_model,
                    g2p_word_transform=self.g2p_word_transform,
                    missing_words_path=self.unknown_words,
                    base_language_model_fst=self.base_language_model_fst,
                    base_language_model_weight=self.base_language_model_weight,
                    mixed_language_model_fst=self.mixed_language_model_fst,
                    spn_phone=self.spn_phone,
                    sil_phone=self.sil_phone,
                    allow_unknown_words=self.allow_unknown_words,
                    frequent_words=self.frequent_words,
                    unk_prob=self.unknown_words_probability,
                    sil_prob=self.silence_probability,
                    unknown_token=self.unknown_token,
                    max_unk_words=self.max_unknown_words,
                    cancel_word=self.cancel_word,
                    cancel_prob=self.cancel_probability,
                )
            else:
                _LOGGER.warning("Not overwriting HCLG.fst")
                kaldi_dir = rhasspyasr_kaldi.get_kaldi_dir()
                rhasspyasr_kaldi.train_prepare_online_decoding(
                    self.model_dir, self.graph_dir, kaldi_dir
                )
            model_url="model.zip"
            model_md5 = self.zip_model(self.model_dir.parents[1] / model_url)
            yield (AsrTrainSuccess(id=train.id,model_md5=model_md5,model_url="profile/"+model_url), {"site_id": site_id})
        except Exception as e:
            _LOGGER.exception("train")
            yield AsrError(error=str(e), site_id=site_id, session_id=train.id)

    def zip_model(self,file):
        if not os.path.isfile(self.model_dir / "conf/model.conf"):
            config = ["--min-active=200", "--max-active=3000", "--beam=10.0", "--lattice-beam=2.0", "--acoustic-scale=1.0", "--frame-subsampling-factor=3", "--endpoint.silence-phones=1:2:3:4:5:6:7:8:9:10", "--endpoint.rule2.min-trailing-silence=0.5", "--endpoint.rule3.min-trailing-silence=1.0", "--endpoint.rule4.min-trailing-silence=2.0"]
            with open(self.model_dir / "conf/model.conf","w") as f:
                for l in config:
                    f.write(l)
                    f.write("\n")
        with zipfile.ZipFile(file=file,mode="w") as zip_file:
            zip_file.write(self.model_dir / "online/final.mdl","am/final.mdl")
            zip_file.write(self.model_dir / "online/conf/mfcc.conf","conf/mfcc.conf")
            zip_file.write(self.model_dir / "conf/model.conf" ,"conf/model.conf")
            zip_file.write(self.model_dir / "online/conf/splice.conf","ivector/splice.conf")
            for root, dirs, files in os.walk(self.model_dir / "online/ivector_extractor") :
                for f in files:
                    if f != "splice_opts" :
                        zip_file.write(os.path.join(root,f),os.path.join("ivector" , f))
            zip_file.write(self.model_dir / "data/lang/G.fst","graph/Gr.fst")
            zip_file.write(self.model_dir / "graph/HCLG.fst","graph/HCLGr.fst")
            for root, dirs, files in os.walk(self.model_dir / "graph") :
                for f in files:
                    if f != "HCLG.fst" :
                        zip_file.write(os.path.join(root,f),os.path.join(os.path.relpath(root,self.model_dir) , f))
        with open(file,"rb") as f:
            return hashlib.md5(f.read()).hexdigest()
 
    async def handle_pronounce(
        self, pronounce: G2pPronounce
    ) -> typing.AsyncIterable[typing.Union[G2pPhonemes, G2pError]]:
        """Looks up or guesses word pronunciation(s)."""
        try:
            result = G2pPhonemes(
                word_phonemes={},
                id=pronounce.id,
                site_id=pronounce.site_id,
                session_id=pronounce.session_id,
            )

            # Load base dictionaries
            pronunciations: PronunciationsType = {}

            for base_dict in self.base_dictionaries:
                if base_dict.path.is_file():
                    _LOGGER.debug("Loading base dictionary from %s", base_dict.path)
                    with open(base_dict.path, "r") as base_dict_file:
                        rhasspynlu.g2p.read_pronunciations(
                            base_dict_file, word_dict=pronunciations
                        )

            # Try to look up in dictionary first
            missing_words: typing.Set[str] = set()
            if pronunciations:
                for word in pronounce.words:
                    # Handle case transformation
                    if self.dictionary_word_transform:
                        word = self.dictionary_word_transform(word)

                    word_prons = pronunciations.get(word)
                    if word_prons:
                        # Use dictionary pronunciations
                        result.word_phonemes[word] = [
                            G2pPronunciation(phonemes=p, guessed=False)
                            for p in word_prons
                        ]
                    else:
                        # Will have to guess later
                        missing_words.add(word)
            else:
                # All words must be guessed
                missing_words.update(pronounce.words)

            if missing_words:
                if self.g2p_model:
                    _LOGGER.debug("Guessing pronunciations of %s", missing_words)
                    guesses = rhasspynlu.g2p.guess_pronunciations(
                        missing_words,
                        self.g2p_model,
                        g2p_word_transform=self.g2p_word_transform,
                        num_guesses=pronounce.num_guesses,
                    )

                    # Add guesses to result
                    for guess_word, guess_phonemes in guesses:
                        result_phonemes = result.word_phonemes.get(guess_word) or []
                        result_phonemes.append(
                            G2pPronunciation(phonemes=guess_phonemes, guessed=True)
                        )
                        result.word_phonemes[guess_word] = result_phonemes
                else:
                    _LOGGER.warning("No g2p model. Cannot guess pronunciations.")

            yield result
        except Exception as e:
            _LOGGER.exception("handle_pronounce")
            yield G2pError(
                error=str(e),
                context=f"model={self.model_dir}, graph={self.graph_dir}",
                site_id=pronounce.site_id,
                session_id=pronounce.session_id,
            )

    # -------------------------------------------------------------------------

    async def on_message_blocking(
        self, message: Message, site_id=None, session_id=None, topic=None
    ) -> GeneratorType:
        """Received message from MQTT broker."""
        if isinstance(message, AsrToggleOn):
            if message.reason == AsrToggleReason.UNKNOWN:
                # Always enable on unknown
                self.disabled_reasons.clear()
            else:
                self.disabled_reasons.discard(message.reason)

            if self.disabled_reasons:
                _LOGGER.debug("Still disabled: %s", self.disabled_reasons)
            else:
                self.enabled = True
                self.first_audio = True
                _LOGGER.debug("Enabled")
        elif isinstance(message, AsrToggleOff):
            self.enabled = False
            self.disabled_reasons.add(message.reason)
            _LOGGER.debug("Disabled (%s)", message.reason)
        elif isinstance(message, AudioFrame):
            if self.enabled:
                # Add to all active sessions
                if self.first_audio:
                    _LOGGER.debug("Receiving audio")
                    self.first_audio = False

                async for frame_result in self.handle_audio_frame(
                    message.wav_bytes, site_id=site_id
                ):
                    yield frame_result
        elif isinstance(message, AudioSessionFrame):
            if self.enabled:
                # Check site_id
                if session_id in self.sessions:
                    if self.first_audio:
                        _LOGGER.debug("Receiving audio")
                        self.first_audio = False

                    # Add to specific session only
                    async for session_frame_result in self.handle_audio_frame(
                        message.wav_bytes, site_id=site_id, session_id=session_id
                    ):
                        yield session_frame_result
        elif isinstance(message, AsrStartListening):
            # hermes/asr/startListening
            async for start_result in self.start_listening(message):
                yield start_result
        elif isinstance(message, AsrStopListening):
            # hermes/asr/stopListening
            async for stop_result in self.stop_listening(message):
                yield stop_result
        elif isinstance(message, AsrTrain):
            # rhasspy/asr/<site_id>/train
            async for train_result in self.handle_train(message, site_id=site_id):
                yield train_result
        elif isinstance(message, G2pPronounce):
            # rhasspy/g2p/pronounce
            async for pronounce_result in self.handle_pronounce(message):
                yield pronounce_result
        else:
            _LOGGER.warning("Unexpected message: %s", message)

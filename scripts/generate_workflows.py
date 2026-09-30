"""Generate explicit ComfyUI API graphs from the pinned official LTX 2.5 recipes.

No UI subgraphs, hosted encoders, downloads, prompt rewriting, or guessed models.
See workflows/PROVENANCE.md for intentional adaptations from the source graphs.
"""

import argparse
import json
from pathlib import Path

from configure_profile import configure_profile

ROOT = Path(__file__).resolve().parents[1]
DISTILLED_SIGMAS = "1.0, 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0"
REFINE_SIGMAS = "0.85, 0.7250, 0.4219, 0.0"
NEGATIVE = "blurry, out of focus, low quality, flickering, distorted proportions, extra limbs, text artifacts"
BASE_MODELS = {
    "model": ("UNETLoader", {"unet_name": "ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors", "weight_dtype": "default"}),
    "clip": ("CLIPLoader", {"clip_name": "gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors", "type": "ltxv", "device": "default"}),
    "video_vae": ("VAELoader", {"vae_name": "ltx-2.5-video-vae-bf16.safetensors"}),
    "audio_vae": ("VAELoader", {"vae_name": "ltx-2.5-audio-vae-bf16.safetensors"}),
}


def ref(node, slot=0):
    return [node, slot]


class Graph:
    def __init__(self, mode, description, source, video=True, two_stage=False):
        self.nodes = {}
        self.meta = {"file": mode + ".json", "description": description, "official_source": source,
                     "defaults": {}, "constraints": {}, "bindings": {}, "media": {},
                     "output_prefix": [{"node_id": "save", "input": "filename_prefix"}],
                     "output_kind": "video" if video else "audio", "stages": 2 if two_stage else 1,
                     "output_scale": 2 if two_stage else 1, "fixed_sampling_steps": 8}
        for name, (cls, inputs) in BASE_MODELS.items():
            if not video and name == "video_vae":
                continue
            self.add(name, cls, **inputs)
        self.add("positive", "CLIPTextEncode", clip=ref("clip"), text="A cinematic scene with natural motion and synchronized ambient sound.")
        self.add("negative", "CLIPTextEncode", clip=ref("clip"), text=NEGATIVE)
        self.bind("prompt", "positive", "text", self.nodes["positive"]["inputs"]["text"], {"type": "string", "max_length": 12000})
        self.bind("negative_prompt", "negative", "text", NEGATIVE, {"type": "string", "max_length": 12000})
        self.add("conditioning", "LTXVConditioning", positive=ref("positive"), negative=ref("negative"), frame_rate=24.0)
        self.bind("fps", "conditioning", "frame_rate", 24, {"type": "integer", "minimum": 1, "maximum": 60})
        self.add("audio_latent", "LTXVEmptyLatentAudio", audio_vae=ref("audio_vae"), frames_number=121, frame_rate=24, batch_size=1)
        self.bind("num_frames", "audio_latent", "frames_number", 121, {"type": "integer", "minimum": 9, "maximum": 241, "multiple_of": 8, "offset": 1})
        self.bind("fps", "audio_latent", "frame_rate")
        if video:
            self.add("video_latent", "EmptyLTXVLatentVideo", width=512 if two_stage else 768,
                     height=320 if two_stage else 512, length=121, batch_size=1)
            self.bind("width", "video_latent", "width", self.nodes["video_latent"]["inputs"]["width"], {"type": "integer", "minimum": 256, "maximum": 1536, "multiple_of": 32})
            self.bind("height", "video_latent", "height", self.nodes["video_latent"]["inputs"]["height"], {"type": "integer", "minimum": 256, "maximum": 1536, "multiple_of": 32})
            self.bind("num_frames", "video_latent", "length")
        else:
            self.add("video_latent", "LTXVAudioOnlyEmptyVideoLatent")
            self.add("audio_only_model", "LTXVAudioOnlyModel", model=ref("model"))
        self.model = ref("model" if video else "audio_only_model")
        self.positive, self.negative = ref("conditioning"), ref("conditioning", 1)
        self.video, self.audio = ref("video_latent"), ref("audio_latent")
        self.original_audio = None
        self.crop_guides = False
        self.two_stage = two_stage

    def add(self, name, cls, **inputs):
        self.nodes[name] = {"class_type": cls, "inputs": inputs}
        return ref(name)

    def bind(self, param, node, input_name, default=None, constraint=None):
        self.meta["bindings"].setdefault(param, []).append({"node_id": node, "input": input_name})
        if default is not None:
            self.meta["defaults"][param] = default
        if constraint:
            self.meta["constraints"][param] = constraint

    def media(self, role, kind, require_audio=False):
        cls, field, placeholder = {"image": ("LoadImage", "image", role + ".png"),
                                   "video": ("LoadVideo", "file", role + ".mp4"),
                                   "audio": ("LoadAudio", "audio", role + ".wav")}[kind]
        name = "load_" + role
        self.add(name, cls, **{field: placeholder})
        self.meta["media"][role] = {"kind": kind, "node_id": name, "input": field, "required": True}
        if require_audio:
            self.meta["media"][role]["require_audio"] = True
        if kind == "video":
            self.add(role + "_components", "GetVideoComponents", video=ref(name))
            self.add(role + "_frames", "ImageFromBatch", image=ref(role + "_components"), batch_index=0, length=121)
            self.bind("num_frames", role + "_frames", "length")
            return ref(role + "_frames")
        return ref(name)

    def resize(self, name, image, method="lanczos"):
        self.add(name, "ImageScale", image=image, upscale_method=method,
                 width=self.meta["defaults"]["width"], height=self.meta["defaults"]["height"], crop="center")
        self.bind("width", name, "width")
        self.bind("height", name, "height")
        return ref(name)

    def image_condition(self, role="image", guide=False, index=0):
        image = self.resize(role + "_resize", self.media(role, "image"))
        image = self.add(role + "_preprocess", "LTXVPreprocess", image=image, img_compression=18)
        self.bind("image_compression", role + "_preprocess", "img_compression", 18, {"type": "integer", "minimum": 0, "maximum": 100})
        name = role + "_condition"
        if guide:
            self.add(name, "LTXVAddGuide", positive=self.positive, negative=self.negative,
                     vae=ref("video_vae"), latent=self.video, image=image, frame_idx=index, strength=0.7)
            self.positive, self.negative, self.video = ref(name), ref(name, 1), ref(name, 2)
            self.crop_guides = True
        else:
            self.video = self.add(name, "LTXVImgToVideoInplace", vae=ref("video_vae"), image=image,
                                  latent=self.video, strength=0.7, bypass=False)
        self.bind(role + "_strength", name, "strength", 0.7, {"type": "number", "minimum": 0, "maximum": 1})
        return image

    def input_audio(self, source=None, ref_tokens=False):
        audio = self.media("audio", "audio") if source is None else source
        self.original_audio = self.add("trim_audio", "TrimAudioDuration", audio=audio, start_index=0.0, duration=121/24)
        self.bind("duration", "trim_audio", "duration")
        self.add("encode_audio", "LTXVAudioVAEEncode", audio=self.original_audio, audio_vae=ref("audio_vae"))
        if ref_tokens:
            self.add("audio_reference", "LTXVSetAudioRefTokens", positive=self.positive, negative=self.negative, audio_latent=ref("encode_audio"))
            self.positive, self.negative, self.audio = ref("audio_reference"), ref("audio_reference", 1), ref("audio_reference", 2)
        else:
            self.add("freeze_audio_mask", "SolidMask", value=0.0, width=1024, height=1024)
            self.audio = self.add("freeze_audio", "SetLatentNoiseMask", samples=ref("encode_audio"), mask=ref("freeze_audio_mask"))

    def iclora(self, filename, image, strength=1.0, advanced=False):
        self.model = self.add("iclora", "LTXICLoRALoaderModelOnly", model=ref("model"), lora_name=filename, strength_model=strength)
        self.bind("lora_strength", "iclora", "strength_model", strength, {"type": "number", "minimum": 0, "maximum": 2})
        inputs = {"positive": self.positive, "negative": self.negative, "vae": ref("video_vae"), "latent": self.video,
                  "image": image, "frame_idx": 0, "strength": 1.0, "latent_downscale_factor": ref("iclora", 1),
                  "crop": "disabled", "use_tiled_encode": True, "tile_size": 256, "tile_overlap": 64}
        if advanced:
            inputs["attention_strength"] = 1.0
        self.add("control", "LTXAddVideoICLoRAGuideAdvanced" if advanced else "LTXAddVideoICLoRAGuide", **inputs)
        self.bind("control_strength", "control", "strength", 1.0, {"type": "number", "minimum": 0, "maximum": 1})
        self.positive, self.negative, self.video = ref("control"), ref("control", 1), ref("control", 2)
        self.crop_guides = True

    def sample(self, stage="sample", model=None, sigmas=DISTILLED_SIGMAS, sampler="euler_ancestral", dual=False):
        self.add(stage + "_av", "LTXVConcatAVLatent", video_latent=self.video, audio_latent=self.audio)
        self.add(stage + "_noise", "RandomNoise", noise_seed=42)
        self.bind("seed", stage + "_noise", "noise_seed", 42, {"type": "integer", "minimum": 0, "maximum": 18446744073709551615})
        self.add(stage + "_sigmas", "ManualSigmas", sigmas=sigmas)
        if dual:
            self.add(stage + "_sampler", "SamplerEulerAncestral", eta=0.0, s_noise=1.0)
            self.add(stage + "_guider", "LTXVDualCFGGuider", model=model or self.model,
                     positive=self.positive, negative=self.negative, video_cfg=1.0, audio_cfg=1.0)
            self.bind("cfg", stage + "_guider", "video_cfg", 1.0, {"type": "number", "enum": [1.0]})
            self.bind("cfg", stage + "_guider", "audio_cfg")
        else:
            self.add(stage + "_sampler", "KSamplerSelect", sampler_name=sampler)
            self.add(stage + "_guider", "CFGGuider", model=model or self.model, positive=self.positive, negative=self.negative, cfg=1.0)
            self.bind("cfg", stage + "_guider", "cfg", 1.0, {"type": "number", "enum": [1.0]})
        self.add(stage, "SamplerCustomAdvanced", noise=ref(stage + "_noise"), guider=ref(stage + "_guider"),
                 sampler=ref(stage + "_sampler"), sigmas=ref(stage + "_sigmas"), latent_image=ref(stage + "_av"))
        self.add(stage + "_split", "LTXVSeparateAVLatent", av_latent=ref(stage, 1 if dual else 0))
        self.video, self.audio = ref(stage + "_split"), ref(stage + "_split", 1)
        if self.crop_guides:
            self.add(stage + "_crop", "LTXVCropGuides", positive=self.positive, negative=self.negative, latent=self.video)
            self.positive, self.negative = ref(stage + "_crop"), ref(stage + "_crop", 1)
            self.video = ref(stage + "_crop", 2)
            self.crop_guides = False

    def upscale(self, image=None):
        self.add("upscaler", "LatentUpscaleModelLoader", model_name="ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors")
        self.video = self.add("upscale", "LTXVLatentUpsampler", samples=self.video, upscale_model=ref("upscaler"), vae=ref("video_vae"))
        if image:
            self.video = self.add("refine_image", "LTXVImgToVideoInplace", vae=ref("video_vae"), image=image, latent=self.video, strength=1.0, bypass=False)
        if self.original_audio:
            self.audio = ref("freeze_audio")
        self.sample("refine", sigmas=REFINE_SIGMAS)

    def decode(self, name="decode_video"):
        return self.add(name, "VAEDecodeTiled", samples=self.video, vae=ref("video_vae"), tile_size=512, overlap=64, temporal_size=64, temporal_overlap=8)

    def output(self, images=None):
        if self.meta["output_kind"] == "audio":
            self.add("decode_audio", "LTXVAudioVAEDecode", samples=self.audio, audio_vae=ref("audio_vae"))
            self.add("save", "SaveAudioAdvanced", audio=ref("decode_audio"), filename_prefix="ltx25/audio", format="flac")
        else:
            if not self.original_audio:
                self.add("decode_audio", "LTXVAudioVAEDecode", samples=self.audio, audio_vae=ref("audio_vae"))
            self.add("video", "CreateVideo", images=images or self.decode(), audio=self.original_audio or ref("decode_audio"), fps=24, bit_depth=8)
            self.bind("fps", "video", "fps")
            self.add("save", "SaveVideo", video=ref("video"), filename_prefix="ltx25/video", format="mp4", **{"format.codec": "h264", "format.codec.encoding": "auto"})
        # Remove orphaned models/latents and their bindings after mode specialization.
        used = set()
        def visit(node):
            if node in used:
                return
            used.add(node)
            for value in self.nodes[node]["inputs"].values():
                if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                    visit(value[0])
        visit("save")
        self.nodes = {k: v for k, v in self.nodes.items() if k in used}
        self.meta["bindings"] = {p: [b for b in bs if b["node_id"] in used] for p, bs in self.meta["bindings"].items()}
        self.meta["bindings"] = {p: bs for p, bs in self.meta["bindings"].items() if bs}
        for key in ("defaults", "constraints"):
            self.meta[key] = {p: v for p, v in self.meta[key].items() if p in self.meta["bindings"]}
        return self


OFFICIAL = "https://github.com/Lightricks/ComfyUI-LTXVideo/blob/15d09abb5a187a8dcaea2fc31fe51ee96e6c9d0d/example_workflows/2.5/"
FLF_SOURCE = "https://github.com/Comfy-Org/workflow_templates/blob/cce0b679980e4215000f67fe7b12c3a1982310ea/templates/video_ltx2_5_flf2v.json"


def generation(mode, image=False, two_stage=False, audio=False, only_audio=False, first_last=False):
    source = "LTX-2.5_T2A_Single_Stage_Distilled.json" if only_audio else ("LTX-2.5_A2V_Two_Stage_Distilled.json" if audio else "LTX-2.5_T2V_I2V_" + ("Two" if two_stage else "Single") + "_Stage_Distilled.json")
    g = Graph(mode, mode.replace("_", " "), FLF_SOURCE if first_last else OFFICIAL + source, video=not only_audio, two_stage=two_stage)
    image_ref = None
    if first_last:
        g.image_condition("first_frame", guide=True, index=0)
        g.image_condition("last_frame", guide=True, index=-1)
    elif image:
        image_ref = g.image_condition()
    if audio:
        g.input_audio()
    g.sample(dual=first_last)
    if two_stage:
        g.upscale(image_ref)
    return g.output()


def control(mode, kind, two_stage=False):
    files = {"canny": "union-control-ref0.5", "preprocessed": "union-control-ref0.5", "deblur": "deblur-0.9", "reference": "ingredients-0.9", "motion": "motion-track-control-ref0.5"}
    sources = {"canny": "ICLoRA_Union_Control_Distilled", "preprocessed": "ICLoRA_Union_Control_Distilled", "deblur": "V2V_ICLoRA_Single_Stage_Distilled", "reference": "ICLoRA_Ingredients_Single_Stage_Distilled", "motion": "ICLoRA_Motion_Track_Distilled"}
    g = Graph(mode, mode.replace("_", " "), OFFICIAL + "LTX-2.5_" + sources[kind] + ".json", two_stage=two_stage)
    if kind in ("canny", "preprocessed", "motion"):
        # ref0.5 adapters use a half-resolution reference grid: latent dimensions
        # must be divisible by two (see LTXAddVideoICLoRAGuide.execute).
        for parameter in ("width", "height"):
            g.meta["constraints"][parameter]["multiple_of"] = 64
    if kind == "reference":
        guide = g.resize("reference_resize", g.media("image", "image"))
        guide = g.add("reference_repeat", "RepeatImageBatch", image=guide, amount=121)
        g.bind("num_frames", "reference_repeat", "amount")
    elif kind == "motion":
        g.image_condition()
        guide = g.add("tracks", "LTXVDrawTracks", tracks=json.dumps([[{"x": 384, "y": 256} for _ in range(121)]]), width=768, height=512)
        g.bind("tracks_json", "tracks", "tracks", g.nodes["tracks"]["inputs"]["tracks"], {"type": "string", "max_length": 200000})
        g.bind("width", "tracks", "width")
        g.bind("height", "tracks", "height")
    else:
        guide = g.resize("source_resize", g.media("video", "video", require_audio=kind == "deblur"))
        if kind == "canny":
            guide = g.add("canny", "Canny", image=guide, low_threshold=0.4, high_threshold=0.8)
            g.bind("canny_low", "canny", "low_threshold", 0.4, {"type": "number", "minimum": 0.01, "maximum": 0.99})
            g.bind("canny_high", "canny", "high_threshold", 0.8, {"type": "number", "minimum": 0.01, "maximum": 0.99})
    g.iclora("ltx-2.3-22b-ic-lora-" + files[kind] + ".safetensors", guide, 1.3 if kind == "reference" else 1.0)
    if kind == "deblur":
        g.input_audio(ref("video_components", 1), ref_tokens=True)
    g.sample()
    if two_stage:
        g.upscale()
    return g.output()


def inpaint(mode, outpaint=False):
    source = "Outpaint" if outpaint else "Inpaint"
    g = Graph(mode, mode.replace("_", " "), OFFICIAL + "LTX-2.5_ICLoRA_" + source + "_Two_Stage_Distilled.json", two_stage=True)
    images = g.resize("source_resize", g.media("video", "video", require_audio=True))
    if outpaint:
        images = g.add("pad_source", "ImagePadForOutpaint", image=images, left=128, right=128, top=96, bottom=96, feathering=0)
        for side in ("left", "right", "top", "bottom"):
            g.bind("pad_" + side, "pad_source", side, g.nodes["pad_source"]["inputs"][side], {"type": "integer", "minimum": 0, "maximum": 512, "multiple_of": 32})
        g.add("padded_size", "GetImageSize", image=images)
        g.nodes["video_latent"]["inputs"].update(width=ref("padded_size"), height=ref("padded_size", 1))
        for parameter in ("width", "height"):
            g.meta["bindings"][parameter] = [b for b in g.meta["bindings"][parameter] if b["node_id"] != "video_latent"]
        mask_image = g.add("mask_image", "MaskToImage", mask=ref("pad_source", 1))
        mask_image = g.add("mask_repeat", "RepeatImageBatch", image=mask_image, amount=121)
        g.bind("num_frames", "mask_repeat", "amount")
    else:
        mask_image = g.resize("mask_resize", g.media("mask_video", "video"), method="nearest-exact")
    mask = g.add("mask", "ImageToMask", image=mask_image, channel="red")
    green = g.add("masked_source", "LTXVInpaintPreprocess", images=images, mask=mask)
    g.iclora("ltx-2.3-22b-ic-lora-in-outpainting-0.9.safetensors", green, advanced=True)
    g.input_audio(ref("video_components", 1), ref_tokens=True)
    g.sample()
    low = g.decode("decode_low")
    low = g.add("blend_low", "LTXVLaplacianPyramidBlend", image_a=low, image_b=images, mask=mask, trim_to_shortest=True, mask_low_res_dilation=5)
    high = g.add("scale_blended", "ImageScaleBy", image=low, upscale_method="lanczos", scale_by=2.0)
    g.video = g.add("encode_high", "VAEEncodeTiled", pixels=high, vae=ref("video_vae"), tile_size=512, overlap=64, temporal_size=64, temporal_overlap=8)
    # The official edit graphs retain the IC-LoRA model during the refinement pass.
    # Guide slots are removed; reattach the frozen source audio to text conditioning.
    g.positive, g.negative = ref("conditioning"), ref("conditioning", 1)
    g.add("refine_audio_reference", "LTXVSetAudioRefTokens", positive=g.positive, negative=g.negative, audio_latent=ref("encode_audio"))
    g.positive, g.negative, g.audio = ref("refine_audio_reference"), ref("refine_audio_reference", 1), ref("refine_audio_reference", 2)
    g.sample("refine", sigmas="0.7250, 0.4219, 0.0", sampler="euler")
    result = g.decode()
    images_high = g.add("source_high", "ImageScaleBy", image=images, upscale_method="lanczos", scale_by=2.0)
    mask_high = g.add("mask_high_image", "ImageScaleBy", image=mask_image, upscale_method="nearest-exact", scale_by=2.0)
    mask_high = g.add("mask_high", "ImageToMask", image=mask_high, channel="red")
    result = g.add("blend_high", "LTXVLaplacianPyramidBlend", image_a=result, image_b=images_high, mask=mask_high, trim_to_shortest=True, mask_low_res_dilation=6)
    return g.output(result)


def pixel_upscale():
    """Official 2.5 pixel IC-LoRA card explicitly prescribes an IC-LoRA V2V graph."""
    mode = "video_upscale_x2"
    g = Graph(mode, "Creative 2x video upscaling with the official LTX 2.5 pixel spatial IC-LoRA",
              "https://huggingface.co/Lightricks/LTX-2.5-22b-IC-LoRA-Pixel-Spatial-Upscaler/blob/b29c1f25902886fd40bc43db7b69e942457298d2/README.md")
    g.meta["adapted_from"] = OFFICIAL + "LTX-2.5_V2V_ICLoRA_Single_Stage_Distilled.json"
    g.meta["output_scale"] = 2
    # API width/height describe the normalized low-resolution source. Sampling
    # takes place directly on the 2x target grid, not via the latent upsampler.
    for parameter, value in (("width", 512), ("height", 288)):
        g.meta["defaults"][parameter] = value
        for binding in g.meta["bindings"][parameter]:
            binding["multiplier"] = 2
            g.nodes[binding["node_id"]]["inputs"][binding["input"]] = value * 2
    guide = g.resize("source_resize", g.media("video", "video"))
    g.meta["media"]["video"]["ensure_audio"] = True
    g.iclora("ltx-2.5-22b-ic-lora-pixel-spatial-upscaler-x2-1.0.safetensors", guide)
    # The model card requires a factor of 2. Do not silently fall back to 1 if
    # adapter metadata is absent: this is a fixed property of this x2 mode.
    g.nodes["control"]["inputs"]["latent_downscale_factor"] = 2.0
    g.input_audio(ref("video_components", 1), ref_tokens=True)
    g.sample()
    return g.output()


def finish_4k(g, mode, width, height, scale, images):
    """Specialize an opt-in, short UHD graph without changing legacy graphs."""
    g.meta.update(file=mode + ".json", admission_profile="4k", output_scale=scale,
                  output_dimensions={"width": 3840, "height": 2160})
    for parameter, value in (("width", width), ("height", height)):
        g.meta["defaults"][parameter] = value
        g.meta["constraints"][parameter] = {"type": "integer", "enum": [value],
                                             "minimum": value, "maximum": value,
                                             "multiple_of": 32}
    g.meta["defaults"]["num_frames"] = 9
    g.meta["constraints"]["num_frames"]["maximum"] = 33
    # These are real generative upscaler properties, not adjustable resize knobs.
    for parameter in ("lora_strength", "control_strength"):
        g.meta["constraints"][parameter] = {"type": "number", "enum": [1.0]}
    for node in g.nodes.values():
        if node["class_type"] == "VAEDecodeTiled":
            node["inputs"].update(tile_size=256, overlap=64,
                                  temporal_size=16, temporal_overlap=8)
    cropped = g.add("crop_uhd", "ImageCrop", image=images,
                    width=3840, height=2160, x=0, y=8)
    # output() has already finalized the source graph once. Replace its output
    # without duplicating the fps binding or overwriting decoded source media.
    for name in ("video", "save"):
        g.nodes.pop(name, None)
    g.meta["bindings"] = {
        parameter: [binding for binding in bindings
                    if binding["node_id"] not in ("video", "save")]
        for parameter, bindings in g.meta["bindings"].items()
    }
    g.output(cropped)
    # Standalone API JSON must agree with the manifest's defaults, including
    # multiplied latent dimensions and duration derived from the short clip.
    values = dict(g.meta["defaults"])
    values["duration"] = values["num_frames"] / values["fps"]
    for parameter, bindings in g.meta["bindings"].items():
        for binding in bindings:
            g.nodes[binding["node_id"]]["inputs"][binding["input"]] = (
                values[parameter] * binding.get("multiplier", 1))
    return g


def video_upscale_4k():
    g = pixel_upscale()
    g.meta["description"] = "Experimental 48 GB qualification: 1920x1088 reference to exact UHD with the LTX 2.5 pixel IC-LoRA"
    return finish_4k(g, "video_upscale_4k", 1920, 1088, 2, ref("decode_video"))


def image_to_video_4k():
    g = generation("image_to_video_4k", image=True, two_stage=True)
    g.meta["description"] = "Experimental 48 GB qualification: two-stage I2V followed by the LTX 2.5 pixel IC-LoRA to exact UHD"
    g.meta["upscale_source"] = "https://huggingface.co/Lightricks/LTX-2.5-22b-IC-LoRA-Pixel-Spatial-Upscaler/blob/b29c1f25902886fd40bc43db7b69e942457298d2/README.md"
    g.meta["stages"] = 3
    # Keep decode_video/decode_audio as the completed 1920x1088 source. All
    # subsequent node IDs are distinct so final decoding cannot form a cycle.
    g.video = g.add("pixel_video_latent", "EmptyLTXVLatentVideo",
                    width=3840, height=2176, length=9, batch_size=1)
    g.bind("num_frames", "pixel_video_latent", "length")
    for parameter in ("width", "height"):
        g.bind(parameter, "pixel_video_latent", parameter)
        g.meta["bindings"][parameter][-1]["multiplier"] = 4
    g.positive, g.negative = ref("conditioning"), ref("conditioning", 1)
    g.iclora("ltx-2.5-22b-ic-lora-pixel-spatial-upscaler-x2-1.0.safetensors",
             ref("decode_video"))
    g.nodes["control"]["inputs"]["latent_downscale_factor"] = 2.0
    g.input_audio(ref("decode_audio"), ref_tokens=True)
    g.sample("pixel")
    return finish_4k(g, "image_to_video_4k", 960, 544, 4,
                     g.decode("pixel_decode_video"))


def image_to_video_native_4k():
    """Direct UHD qualification: one full-resolution distilled sampling pass."""
    mode = "image_to_video_native_4k"
    g = generation(mode, image=True)
    g.meta.update(
        description="Experimental native UHD qualification: one 3840x2176 image-to-video diffusion pass, then a center crop",
        admission_profile="native_4k", output_scale=1,
        output_dimensions={"width": 3840, "height": 2160},
    )
    for parameter, value in (("width", 3840), ("height", 2176),
                             ("num_frames", 9), ("fps", 24)):
        g.meta["defaults"][parameter] = value
        g.meta["constraints"][parameter] = {
            "type": "integer", "enum": [value], "minimum": value, "maximum": value,
        }
    # Retain the proven short default; expose only the specifically requested
    # 121-frame qualification as the additional direct-UHD workload.
    g.meta["constraints"]["num_frames"].update(enum=[9, 121], maximum=121)
    # The official inplace image-conditioning node retains its native behavior.
    # Pinned ComfyUI vae.encode automatically retries tiled encoding on OOM.
    g.nodes["decode_video"]["inputs"].update(
        tile_size=256, overlap=64, temporal_size=16, temporal_overlap=8,
    )
    g.add("crop_uhd", "ImageCrop", image=ref("decode_video"),
          width=3840, height=2160, x=0, y=8)
    g.nodes["video"]["inputs"]["images"] = ref("crop_uhd")
    values = dict(g.meta["defaults"])
    for parameter, bindings in g.meta["bindings"].items():
        for binding in bindings:
            g.nodes[binding["node_id"]]["inputs"][binding["input"]] = values[parameter]
    return g


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("int8", "bf16"), default="int8")
    args = parser.parse_args()
    modes = {
        "text_to_video": generation("text_to_video"),
        "image_to_video": generation("image_to_video", image=True),
        "first_last_frame": generation("first_last_frame", first_last=True),
        "text_to_video_2stage": generation("text_to_video_2stage", two_stage=True),
        "image_to_video_2stage": generation("image_to_video_2stage", image=True, two_stage=True),
        "audio_to_video": generation("audio_to_video", audio=True, two_stage=True),
        "image_audio_to_video": generation("image_audio_to_video", audio=True, image=True, two_stage=True),
        "text_to_audio": generation("text_to_audio", only_audio=True),
        "video_to_video": control("video_to_video", "canny"),
        "video_to_video_2stage": control("video_to_video_2stage", "canny", two_stage=True),
        "video_control": control("video_control", "preprocessed"),
        "video_control_2stage": control("video_control_2stage", "preprocessed", two_stage=True),
        "video_deblur": control("video_deblur", "deblur"),
        "reference_to_video": control("reference_to_video", "reference"),
        "motion_track": control("motion_track", "motion"),
        "video_inpaint": inpaint("video_inpaint"),
        "video_outpaint": inpaint("video_outpaint", outpaint=True),
        "video_upscale_x2": pixel_upscale(),
        "video_upscale_4k": video_upscale_4k(),
        "image_to_video_4k": image_to_video_4k(),
        "image_to_video_native_4k": image_to_video_native_4k(),
    }
    folder = ROOT / "workflows"
    folder.mkdir(exist_ok=True)
    manifest = {"schema_version": 1, "model_family": "LTX-2.5", "precision": "int8", "modes": {}}
    for name, graph in modes.items():
        (folder / graph.meta["file"]).write_text(json.dumps(graph.nodes, indent=2) + "\n", encoding="utf-8")
        manifest["modes"][name] = graph.meta
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    configure_profile(ROOT, args.profile)
    print(f"Wrote {len(modes)} API workflows for {args.profile} and workflows/manifest.json")


if __name__ == "__main__":
    main()

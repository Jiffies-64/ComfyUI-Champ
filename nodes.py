import argparse
import logging
import os
import folder_paths
comfy_path = os.path.dirname(folder_paths.__file__)
node_path = folder_paths.get_folder_paths("custom_nodes")[0]
import os.path as osp
from datetime import datetime
from pathlib import Path

import numpy as np
import torch
import torch.utils.checkpoint
from torchvision import transforms
from diffusers import AutoencoderKL, DDIMScheduler
from diffusers.utils.import_utils import is_xformers_available
from omegaconf import OmegaConf
from PIL import Image
from transformers import CLIPVisionModelWithProjection

from .models.unet_2d_condition import UNet2DConditionModel
from .models.unet_3d import UNet3DConditionModel
from .models.mutual_self_attention import ReferenceAttentionControl
from .models.guidance_encoder import GuidanceEncoder
from .models.champ_model import ChampModel

from .pipelines.pipeline_aggregation import MultiGuidance2LongVideoPipeline

from .utils.video_utils import resize_tensor_frames, save_videos_grid, pil_list_to_tensor, get_images
import json


def setup_savedir(cfg):
    time_str = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    if cfg.exp_name is None:
        savedir = f"results/exp-{time_str}"
    else:
        savedir = f"results/{cfg.exp_name}-{time_str}"
    
    os.makedirs(savedir, exist_ok=True)
    
    return savedir

def setup_guidance_encoder(cfg):
    guidance_encoder_group = dict()
    
    if cfg.weight_dtype == "fp16":
        weight_dtype = torch.float16
    elif cfg.weight_dtype == "bf16":
        weight_dtype = torch.bfloat16
    elif cfg.weight_dtype == "float8_e4m3fn":
        weight_dtype = torch.float8_e4m3fn
    elif cfg.weight_dtype == "float8_e5m2":
        weight_dtype = torch.float8_e5m2
    else:
        weight_dtype = torch.float32
    
    for guidance_type in cfg.guidance_types:
        guidance_encoder_group[guidance_type] = GuidanceEncoder(
            guidance_embedding_channels=cfg.guidance_encoder_kwargs.guidance_embedding_channels,
            guidance_input_channels=cfg.guidance_encoder_kwargs.guidance_input_channels,
            block_out_channels=cfg.guidance_encoder_kwargs.block_out_channels,
        ).to(device="cuda", dtype=weight_dtype)
    
    return guidance_encoder_group

def process_semantic_map(semantic_map_path: Path):
    image_name = semantic_map_path.name
    mask_path = semantic_map_path.parent.parent / "mask" / image_name
    semantic_array = np.array(Image.open(semantic_map_path))
    mask_array = np.array(Image.open(mask_path).convert("RGB"))
    semantic_pil = Image.fromarray(np.where(mask_array > 0, semantic_array, 0))
    
    return semantic_pil

def combine_guidance_data(cfg):
    guidance_types = cfg.guidance_types
    guidance_data_folder = cfg.data.guidance_data_folder
    
    guidance_pil_group = dict()
    for guidance_type in guidance_types:
        guidance_pil_group[guidance_type] = []
        for guidance_image_path in sorted(Path(osp.join(guidance_data_folder, guidance_type)).iterdir()):
            # Add black background to semantic map
            if guidance_type == "semantic_map":
                guidance_pil_group[guidance_type] += [process_semantic_map(guidance_image_path)]
            else:
                guidance_pil_group[guidance_type] += [Image.open(guidance_image_path).convert("RGB")]
    
    # get video length from the first guidance sequence
    first_guidance_length = len(list(guidance_pil_group.values())[0])
    # ensure all guidance sequences are of equal length
    assert all(len(sublist) == first_guidance_length for sublist in list(guidance_pil_group.values()))
    
    return guidance_pil_group, first_guidance_length

def inference(
    cfg,
    vae,
    image_enc,
    model,
    scheduler,
    ref_image_pil,
    guidance_pil_group,
    video_length,
    width,
    height,
    device="cuda",
    dtype=torch.float16,
):
    reference_unet = model.reference_unet
    denoising_unet = model.denoising_unet
    guidance_types = cfg.guidance_types
    guidance_encoder_group = {f"guidance_encoder_{g}": getattr(model, f"guidance_encoder_{g}") for g in guidance_types}
    
    generator = torch.Generator(device=device)
    generator.manual_seed(cfg.seed)
    pipeline = MultiGuidance2LongVideoPipeline(
        vae=vae,
        image_encoder=image_enc,
        reference_unet=reference_unet,
        denoising_unet=denoising_unet,
        **guidance_encoder_group,
        scheduler=scheduler,
    )
    pipeline = pipeline.to(device, dtype)
    
    video = pipeline(
        ref_image_pil,
        guidance_pil_group,
        width,
        height,
        video_length,
        num_inference_steps=cfg.num_inference_steps,
        guidance_scale=cfg.guidance_scale,
        generator=generator
    ).videos
    
    del pipeline
    torch.cuda.empty_cache()
    
    return video

champ_path=f'{node_path}/ComfyUI-Champ'
config_path=f'{champ_path}/configs/inference.yaml'
class ChampLoader:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "sd_path": ("STRING", {"default": "/home/admin/ComfyUI/models/diffusers/stable-diffusion-v1-5"}),
                "vae_path": ("STRING", {"default": "/home/admin/ComfyUI/models/diffusers/sd-vae-ft-mse"}),
                "image_encoder_path": ("STRING", {"default": "/home/admin/ComfyUI/models/diffusers/sd-image-variations-diffusers/image_encoder"}),
                "motion_module_path": ("STRING", {"default": "/home/admin/ComfyUI/models/champ/motion_module.pth"}),
                "denoising_unet_path": ("STRING", {"default": "/home/admin/ComfyUI/models/champ/denoising_unet.pth"}),
                "reference_unet_path": ("STRING", {"default": "/home/admin/ComfyUI/models/champ/reference_unet.pth"}),
                "weight_dtype": (["fp16","fp32"], {"default": "fp16"}),
            },
            "optional": {
                "depth_path": ("STRING", {"default": "none"}),
                "dwpose_path": ("STRING", {"default": "none"}),
                "normal_path": ("STRING", {"default": "none"}),
                "softedge_path": ("STRING", {"default": "none"}),
                "lineart_path": ("STRING", {"default": "none"}),
                "semantic_map_path": ("STRING", {"default": "none"}),
            }
        }

    RETURN_TYPES = ("Champ","cfg","vae","image_enc","noise_scheduler",)
    RETURN_NAMES = ("champ","cfg","vae","image_enc","noise_scheduler",)
    FUNCTION = "run"
    CATEGORY = "Champ"

    def run(self,sd_path,vae_path,image_encoder_path,motion_module_path,denoising_unet_path,reference_unet_path,weight_dtype,depth_path,dwpose_path,softedge_path,lineart_path,normal_path,semantic_map_path):
        cfg = OmegaConf.load(config_path)
        OmegaConf.update(cfg, "base_model_path", sd_path)
        OmegaConf.update(cfg, "vae_model_path", vae_path)
        OmegaConf.update(cfg, "image_encoder_path", image_encoder_path)
        OmegaConf.update(cfg, "motion_module_path", motion_module_path)
        OmegaConf.update(cfg, "weight_dtype", weight_dtype)
        #guidance_types=json.loads(guidance_types)
        #OmegaConf.update(cfg, "guidance_types", guidance_types)
        
        if cfg.weight_dtype == "fp16":
            weight_dtype = torch.float16
        elif cfg.weight_dtype == "bf16":
            weight_dtype = torch.bfloat16
        elif cfg.weight_dtype == "float8_e4m3fn":
            weight_dtype = torch.float8_e4m3fn
        elif cfg.weight_dtype == "float8_e5m2":
            weight_dtype = torch.float8_e5m2
        else:
            weight_dtype = torch.float32
            
        sched_kwargs = OmegaConf.to_container(cfg.noise_scheduler_kwargs)
        if cfg.enable_zero_snr:
            sched_kwargs.update( 
                rescale_betas_zero_snr=True,
                timestep_spacing="trailing",
                prediction_type="v_prediction",
            )
        noise_scheduler = DDIMScheduler(**sched_kwargs)
        sched_kwargs.update({"beta_schedule": "scaled_linear"})
        
        image_enc = CLIPVisionModelWithProjection.from_pretrained(
            cfg.image_encoder_path,
        ).to(dtype=weight_dtype, device="cuda")
        
        vae = AutoencoderKL.from_pretrained(cfg.vae_model_path).to(
            dtype=weight_dtype, device="cuda"
        )
        
        denoising_unet = UNet3DConditionModel.from_pretrained_2d(
            cfg.base_model_path,
            cfg.motion_module_path,
            subfolder="unet",
            unet_additional_kwargs=cfg.unet_additional_kwargs,
        ).to(dtype=weight_dtype, device="cuda")
        
        reference_unet = UNet2DConditionModel.from_pretrained(
            cfg.base_model_path,
            subfolder="unet",
        ).to(device="cuda", dtype=weight_dtype)
        
        guidance_encoder_group = setup_guidance_encoder(cfg)
        
        ckpt_dir = cfg.ckpt_dir
        denoising_unet.load_state_dict(
            torch.load(
                denoising_unet_path,
                map_location="cpu",
            ),
            strict=False,
        )
        reference_unet.load_state_dict(
            torch.load(
                reference_unet_path,
                map_location="cpu",
            ),
            strict=False,
        )
        
        for guidance_type, guidance_encoder_module in guidance_encoder_group.items():
            if guidance_type=="depth" and depth_path != "none":
                guidance_encoder_module.load_state_dict(
                    torch.load(
                        depth_path,
                        map_location="cpu",
                    ),
                    strict=False,
                )
            if guidance_type=="normal" and normal_path != "none":
                guidance_encoder_module.load_state_dict(
                    torch.load(
                        normal_path,
                        map_location="cpu",
                    ),
                    strict=False,
                )
            if guidance_type=="semantic_map" and semantic_map_path != "none":
                guidance_encoder_module.load_state_dict(
                    torch.load(
                        semantic_map_path,
                        map_location="cpu",
                    ),
                    strict=False,
                )
            if guidance_type=="dwpose" and dwpose_path != "none":
                guidance_encoder_module.load_state_dict(
                    torch.load(
                        dwpose_path,
                        map_location="cpu",
                    ),
                    strict=False,
                )
            if guidance_type=="softedge" and softedge_path != "none":
                guidance_encoder_module.load_state_dict(
                    torch.load(
                        softedge_path,
                        map_location="cpu",
                    ),
                    strict=False,
                )
            if guidance_type=="lineart" and lineart_path != "none":
                guidance_encoder_module.load_state_dict(
                    torch.load(
                        lineart_path,
                        map_location="cpu",
                    ),
                    strict=False,
                )
            
        reference_control_writer = ReferenceAttentionControl(
            reference_unet,
            do_classifier_free_guidance=False,
            mode="write",
            fusion_blocks="full",
        )
        reference_control_reader = ReferenceAttentionControl(
            denoising_unet,
            do_classifier_free_guidance=False,
            mode="read",
            fusion_blocks="full",
        )
            
        model = ChampModel(
            reference_unet=reference_unet,
            denoising_unet=denoising_unet,
            reference_control_writer=reference_control_writer,
            reference_control_reader=reference_control_reader,
            guidance_encoder_group=guidance_encoder_group,
        ).to("cuda", dtype=weight_dtype)
        
        if cfg.enable_xformers_memory_efficient_attention:
            if is_xformers_available():
                reference_unet.enable_xformers_memory_efficient_attention()
                denoising_unet.enable_xformers_memory_efficient_attention()
            else:
                raise ValueError(
                    "xformers is not available. Make sure it is installed correctly"
                )
        return (model,cfg,vae,image_enc,noise_scheduler,)


class ChampRun:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("Champ",),
                "cfg": ("cfg",),
                "vae": ("vae",),
                "image_enc": ("image_enc",),
                "noise_scheduler": ("noise_scheduler",),
                "image": ("IMAGE",),
                "width": ("INT",{"default":512}),
                "height": ("INT",{"default":512}),
                "video_length": ("INT",{"default":16}),
                "num_inference_steps": ("INT",{"default":20}),
                "guidance_scale": ("FLOAT",{"default":3.5}),
                "seed": ("INT",{"default":1234}),
            },
            "optional": {
                "depth_images": ("IMAGE",),
                "normal_images": ("IMAGE",),
                "semantic_map_images": ("IMAGE",),
                "dwpose_images": ("IMAGE",),
                "softedge_images": ("IMAGE",),
                "lineart_images": ("IMAGE",),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "run"
    CATEGORY = "Champ"

    def run(self,model,cfg,vae,image_enc,noise_scheduler,image,width,height,video_length,num_inference_steps,guidance_scale,seed,depth_images=None,normal_images=None,semantic_map_images=None,dwpose_images=None,softedge_images=None,lineart_images=None):
        ref_image = 255.0 * image[0].cpu().numpy()
        ref_image_pil = Image.fromarray(np.clip(ref_image, 0, 255).astype(np.uint8))
        ref_image_w, ref_image_h = ref_image_pil.size

        OmegaConf.update(cfg, "width", width)
        OmegaConf.update(cfg, "height", height)
        OmegaConf.update(cfg, "num_inference_steps", num_inference_steps)
        OmegaConf.update(cfg, "guidance_scale", guidance_scale)
        OmegaConf.update(cfg, "seed", seed)
        
        guidance_pil_group=dict()
        if "depth" in cfg.guidance_types and depth_images is not None:
            guidance_pil_group["depth"]=[Image.fromarray(np.clip(255.0*img.cpu().numpy(), 0, 255).astype(np.uint8)) for img in depth_images]
        if "normal" in cfg.guidance_types and normal_images is not None:
            guidance_pil_group["normal"]=[Image.fromarray(np.clip(255.0*img.cpu().numpy(), 0, 255).astype(np.uint8)) for img in normal_images]
        if "semantic_map" in cfg.guidance_types and semantic_map_images is not None:
            guidance_pil_group["semantic_map"]=[Image.fromarray(np.clip(255.0*img.cpu().numpy(), 0, 255).astype(np.uint8)) for img in semantic_map_images]
        if "dwpose" in cfg.guidance_types and dwpose_images is not None:
            guidance_pil_group["dwpose"]=[Image.fromarray(np.clip(255.0*img.cpu().numpy(), 0, 255).astype(np.uint8)) for img in dwpose_images]
        if "softedge" in cfg.guidance_types and softedge_images is not None:
            guidance_pil_group["softedge"]=[Image.fromarray(np.clip(255.0*img.cpu().numpy(), 0, 255).astype(np.uint8)) for img in softedge_images]
        if "lineart" in cfg.guidance_types and lineart_images is not None:
            guidance_pil_group["lineart"] = [Image.fromarray(np.clip(255.0 * img.cpu().numpy(), 0, 255).astype(np.uint8)) for img in lineart_images]

        if cfg.weight_dtype == "fp16":
            weight_dtype = torch.float16
        elif cfg.weight_dtype == "bf16":
            weight_dtype = torch.bfloat16
        elif cfg.weight_dtype == "float8_e4m3fn":
            weight_dtype = torch.float8_e4m3fn
        elif cfg.weight_dtype == "float8_e5m2":
            weight_dtype = torch.float8_e5m2
        else:
            weight_dtype = torch.float32
        
        result_video_tensor = inference(
            cfg=cfg,
            vae=vae,
            image_enc=image_enc,
            model=model,
            scheduler=noise_scheduler,
            ref_image_pil=ref_image_pil,
            guidance_pil_group=guidance_pil_group,
            video_length=video_length,
            width=cfg.width, height=cfg.height,
            device="cuda", dtype=weight_dtype
        )  # (1, c, f, h, w)
        
        result_video_tensor = resize_tensor_frames(result_video_tensor, (ref_image_h, ref_image_w))
        
        return get_images(result_video_tensor)

class ImageCombineOneRow:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "image1": ("IMAGE",),
            "image2": ("IMAGE",),  
        },
        "optional":{
            "image3": ("IMAGE",),
            "image4": ("IMAGE",),
            "image5": ("IMAGE",),
            "image6": ("IMAGE",),
            "image7": ("IMAGE",),
            "image8": ("IMAGE",),
            "image9": ("IMAGE",),
            "image10": ("IMAGE",),
            "image11": ("IMAGE",),
            "image12": ("IMAGE",),
        }}

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "run"
    CATEGORY = "Champ"

    def run(self, image1, image2, image3=None, image4=None, image5=None, image6=None, image7=None, image8=None, image9=None, image10=None, image11=None, image12=None):
        imgs=(image1,image2)
        for img in (image3,image4,image5,image6,image7,image8,image9,image10,image11,image12):
            if img is None:
                break
            else:
                imgs=imgs+(img,)
        row = torch.cat(imgs, dim=2)
        return (row,)

class ImageCombineOneColumn:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "image1": ("IMAGE",),
            "image2": ("IMAGE",),  
        },
        "optional":{
            "image3": ("IMAGE",),
            "image4": ("IMAGE",),
            "image5": ("IMAGE",),
            "image6": ("IMAGE",),
            "image7": ("IMAGE",),
            "image8": ("IMAGE",),
            "image9": ("IMAGE",),
            "image10": ("IMAGE",),
            "image11": ("IMAGE",),
            "image12": ("IMAGE",),
        }}

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "run"
    CATEGORY = "Champ"

    def run(self, image1, image2, image3=None, image4=None, image5=None, image6=None, image7=None, image8=None, image9=None, image10=None, image11=None, image12=None):
        imgs=(image1,image2)
        for img in (image3,image4,image5,image6,image7,image8,image9,image10,image11,image12):
            if img is None:
                break
            else:
                imgs=imgs+(img,)
        col = torch.cat(imgs, dim=1)
        return (col,)
    
NODE_CLASS_MAPPINGS = {
    "ChampLoader":ChampLoader,
    "ChampRun":ChampRun,
    "ImageCombineOneRow":ImageCombineOneRow,
    "ImageCombineOneColumn":ImageCombineOneColumn,
}
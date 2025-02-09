from comfy.ldm.flux.layers import DoubleStreamBlock as DSBold
import copy
import torch
from .xflux.src.flux.modules.layers import DoubleStreamBlock as DSBnew
from .layers import DoubleStreamBlockLoraProcessor, DoubleStreamBlockProcessor, DoubleStreamBlockLorasMixerProcessor

from comfy.utils import get_attr, set_attr
        
def CopyDSB(oldDSB):
    
    if isinstance(oldDSB, DSBold):
        tyan = copy.copy(oldDSB)
        
        mlp_hidden_dim  = tyan.img_mlp[0].out_features
        mlp_ratio = mlp_hidden_dim / tyan.hidden_size
        bi = DSBnew(hidden_size=tyan.hidden_size, num_heads=tyan.num_heads, mlp_ratio=mlp_ratio)
        #better use __dict__ but I bit scared 
        (
            bi.img_mod, bi.img_norm1, bi.img_attn, bi.img_norm2,
            bi.img_mlp, bi.txt_mod, bi.txt_norm1, bi.txt_attn, bi.txt_norm2, bi.txt_mlp
        ) = (
            tyan.img_mod, tyan.img_norm1, tyan.img_attn, tyan.img_norm2,
            tyan.img_mlp, tyan.txt_mod, tyan.txt_norm1, tyan.txt_attn, tyan.txt_norm2, tyan.txt_mlp
        )
        bi.set_processor(DoubleStreamBlockProcessor())

        return bi
    return oldDSB
    
def copy_model(orig, new):
    new = copy.copy(new)
    new.model = copy.copy(orig.model)
    new.model.diffusion_model = copy.copy(orig.model.diffusion_model)
    new.model.diffusion_model.double_blocks = copy.deepcopy(orig.model.diffusion_model.double_blocks)
    count = len(new.model.diffusion_model.double_blocks)
    for i in range(count):
        new.model.diffusion_model.double_blocks[i] = copy.copy(orig.model.diffusion_model.double_blocks[i])
        new.model.diffusion_model.double_blocks[i].load_state_dict(orig.model.diffusion_model.double_blocks[0].state_dict())
    
def FluxUpdateModules(flux_model):
    save_list = {}
    #print((flux_model.diffusion_model.double_blocks))
    #for k,v in flux_model.diffusion_model.double_blocks:
        #if "double" in k:
    count = len(flux_model.diffusion_model.double_blocks)
    patches = {}
    
    for i in range(count):
        patches[f"double_blocks.{i}"]=CopyDSB(flux_model.diffusion_model.double_blocks[i])
        flux_model.diffusion_model.double_blocks[i]=CopyDSB(flux_model.diffusion_model.double_blocks[i])
    return patches
        
def is_model_pathched(model):
    def test(mod):
        if isinstance(mod, DSBnew):
            return True
        else:
            for p in mod.children():
                if test(p):
                    return True
        return False
    result = test(model)
    return result



def attn_processors(model_flux):
    # set recursively
    processors = {}

    def fn_recursive_add_processors(name: str, module: torch.nn.Module, procs):
        
        if hasattr(module, "set_processor"):
            procs[f"{name}.processor"] = module.processor
        for sub_name, child in module.named_children():
            fn_recursive_add_processors(f"{name}.{sub_name}", child, procs)

        return procs

    for name, module in model_flux.named_children():
        fn_recursive_add_processors(name, module, processors)
    return processors
def merge_loras(lora1, lora2):
    new_block = DoubleStreamBlockLorasMixerProcessor()
    if isinstance(lora1, DoubleStreamBlockLorasMixerProcessor):
        new_block.set_loras(*lora1.get_loras())
    elif isinstance(lora1, DoubleStreamBlockLoraProcessor):
        new_block.add_lora(lora1)
    else:
        pass
    if isinstance(lora2, DoubleStreamBlockLorasMixerProcessor):
        new_block.set_loras(*lora2.get_loras())
    elif isinstance(lora2, DoubleStreamBlockLoraProcessor):
        new_block.add_lora(lora2)
    else:
        pass
    return new_block
        
def set_attn_processor(model_flux, processor):
    r"""
    Sets the attention processor to use to compute attention.

    Parameters:
        processor (`dict` of `AttentionProcessor` or only `AttentionProcessor`):
            The instantiated processor class or a dictionary of processor classes that will be set as the processor
            for **all** `Attention` layers.

            If `processor` is a dict, the key needs to define the path to the corresponding cross attention
            processor. This is strongly recommended when setting trainable attention processors.

    """
    count = len(attn_processors(model_flux).keys())
    if isinstance(processor, dict) and len(processor) != count:
        raise ValueError(
            f"A dict of processors was passed, but the number of processors {len(processor)} does not match the"
            f" number of attention layers: {count}. Please make sure to pass {count} processor classes."
        )

    def fn_recursive_attn_processor(name: str, module: torch.nn.Module, processor):
        if hasattr(module, "set_processor"):
            if isinstance(module.get_processor(), DoubleStreamBlockLorasMixerProcessor):
                block = copy.copy(module.get_processor())
                module.set_processor(copy.deepcopy(module.get_processor()))
                new_block = DoubleStreamBlockLorasMixerProcessor()
                #q1, q2, p1, p2, w1 = block.get_loras()
                new_block.set_loras(*block.get_loras())
                if not isinstance(processor, dict):
                    new_block.add_lora(processor)
                else:
                    
                    new_block.add_lora(processor.pop(f"{name}.processor"))
                module.set_processor(new_block)
                #block = set_attr(module, "", new_block)
            elif isinstance(module.get_processor(), DoubleStreamBlockLoraProcessor):
                block = DoubleStreamBlockLorasMixerProcessor()
                block.add_lora(copy.copy(module.get_processor()))
                if not isinstance(processor, dict):
                    block.add_lora(processor)
                else:
                    block.add_lora(processor.pop(f"{name}.processor"))
                module.set_processor(block)
            else:
                if not isinstance(processor, dict):
                    module.set_processor(processor)
                else:
                    module.set_processor(processor.pop(f"{name}.processor"))

        for sub_name, child in module.named_children():
            fn_recursive_attn_processor(f"{name}.{sub_name}", child, processor)

    for name, module in model_flux.named_children():
        fn_recursive_attn_processor(name, module, processor)

import torch
from PIL import Image

def tensor_to_pil(tensor):
    # Убедитесь, что тензор имеет правильную форму
    if tensor.dim() != 4 or tensor.size(0) < 1:
        raise ValueError("Тензор должен иметь форму [batch, h, w, c]")
    
    # Извлекаем первую картинку из батча
    img_tensor = tensor[0]  # [h, w, c]
    
    # Определяем диапазон значений
    value_range = (img_tensor.min(), img_tensor.max())
    
    # Преобразуем тензор в numpy массив
    img_numpy = img_tensor.numpy()  # [c, h, w] -> [h, w, c]
    
    # Нормализуем значения в диапазон [0, 1], если необходимо
    if value_range != (0, 1):
        img_numpy = (img_numpy - value_range[0]) / (value_range[1] - value_range[0])
    
    # Создаем изображение PIL
    img_pil = Image.fromarray((img_numpy * 255).astype('uint8'))
    
    return img_pil


class LATENT_PROCESSOR_COMFY:
    def __init__(self):        
        self.scale_factor = 0.3611
        self.shift_factor = 0.1159
        self.latent_rgb_factors =[
                    [-0.0404,  0.0159,  0.0609],
                    [ 0.0043,  0.0298,  0.0850],
                    [ 0.0328, -0.0749, -0.0503],
                    [-0.0245,  0.0085,  0.0549],
                    [ 0.0966,  0.0894,  0.0530],
                    [ 0.0035,  0.0399,  0.0123],
                    [ 0.0583,  0.1184,  0.1262],
                    [-0.0191, -0.0206, -0.0306],
                    [-0.0324,  0.0055,  0.1001],
                    [ 0.0955,  0.0659, -0.0545],
                    [-0.0504,  0.0231, -0.0013],
                    [ 0.0500, -0.0008, -0.0088],
                    [ 0.0982,  0.0941,  0.0976],
                    [-0.1233, -0.0280, -0.0897],
                    [-0.0005, -0.0530, -0.0020],
                    [-0.1273, -0.0932, -0.0680]
                ]
    def __call__(self, x):
        return (x / self.scale_factor) + self.shift_factor


def check_is_comfy_lora(sd):
    for k in sd:
        if "lora_down" in k or "lora_up" in k:
            return True
    return False

def comfy_to_xlabs_lora(sd):
    sd_out = {}
    for k in sd:
        if "diffusion_model" in k:
            new_k =  (k
                    .replace(".lora_down.weight", ".down.weight")
                    .replace(".lora_up.weight", ".up.weight")
                    .replace(".img_attn.proj.", ".processor.proj_lora1.")
                    .replace(".txt_attn.proj.", ".processor.proj_lora2.")
                    .replace(".img_attn.qkv.", ".processor.qkv_lora1.")
                    .replace(".txt_attn.qkv.", ".processor.qkv_lora2."))
            new_k = new_k[len("diffusion_model."):]
        else:
            new_k=k
        sd_out[new_k] = sd[k]
    return sd_out

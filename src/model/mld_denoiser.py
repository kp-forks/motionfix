import torch
import torch.nn as nn
from torch import  nn
from src.model.utils.timestep_embed import TimestepEmbedding, Timesteps, TimestepEmbedderMDM
from src.model.utils.positional_encoding import PositionalEncoding
from src.model.utils.transf_utils import SkipTransformerEncoder, TransformerEncoderLayer
from src.model.utils.all_positional_encodings import build_position_encoding
from src.data.tools.tensors import lengths_to_mask
from src.model.utils.timestep_embed import TimestepEmbedderMDM

class MldDenoiser(nn.Module):

    def __init__(self,
                 nfeats: int = 263,
                 condition: str = "text",
                 latent_dim: list = [1, 256],
                 ff_size: int = 1024,
                 num_layers: int = 9,
                 num_heads: int = 4,
                 dropout: float = 0.1,
                 normalize_before: bool = False,
                 activation: str = "gelu",
                 flip_sin_to_cos: bool = True,
                 position_embedding: str = "actor",
                 arch: str = "trans_enc",
                 freq_shift: int = 0,
                 text_encoded_dim: int = 768,
                 use_deltas: bool = False,
                 pred_delta_motion: bool = False,
                 fuse: str = None,
                 time_in: str = 'concat',
                 **kwargs) -> None:

        super().__init__()
        self.use_deltas = use_deltas
        self.latent_dim = latent_dim
        self.pred_delta_motion = pred_delta_motion
        self.text_encoded_dim = text_encoded_dim
        self.condition = condition
        self.abl_plus = False
        self.ablation_skip_connection = False
        self.diffusion_only = True
        self.time_fusion = time_in
        self.arch = arch
        self.pe_type = position_embedding
        self.fuse = fuse
        self.feat_comb_coeff = nn.Parameter(torch.tensor([1.0]))
        # if self.diffusion_only:
            # assert self.arch == "trans_enc", "only implement encoder for diffusion-only"
        self.pose_proj_in = nn.Linear(nfeats, self.latent_dim)
        if self.fuse == 'concat':
            self.pose_proj_out = nn.Linear(2*self.latent_dim, nfeats)
        else:
            self.pose_proj_out = nn.Linear(self.latent_dim, nfeats)            
        self.first_pose_proj = nn.Linear(self.latent_dim, nfeats)


        # emb proj
        if self.condition in ["text", "text_uncond"]:
            # text condition
            # project time from text_encoded_dim to latent_dim
            self.time_proj = Timesteps(text_encoded_dim, flip_sin_to_cos,
                                       freq_shift)
            self.time_embedding = TimestepEmbedding(text_encoded_dim,
                                                    self.latent_dim)
            self.embed_timestep = TimestepEmbedderMDM(self.latent_dim)

            # FIXME me TODO this            
            # self.time_embedding = TimestepEmbedderMDM(self.latent_dim)
            
            # project time+text to latent_dim
            if text_encoded_dim != self.latent_dim:
                # todo 10.24 debug why relu
                self.emb_proj = nn.Sequential(
                    nn.ReLU(), nn.Linear(text_encoded_dim, self.latent_dim))
        else:
            raise TypeError(f"condition type {self.condition} not supported")

        if self.pe_type == "actor":
            self.query_pos = PositionalEncoding(self.latent_dim, dropout)
            self.mem_pos = PositionalEncoding(self.latent_dim, dropout)
        elif self.pe_type == "mld":
            self.query_pos = build_position_encoding(
                self.latent_dim, position_embedding='learned')
            self.mem_pos = build_position_encoding(
                self.latent_dim, position_embedding='learned')
        else:
            raise ValueError("Not Support PE type")


        if self.ablation_skip_connection:
            # use DETR transformer
            encoder_layer = TransformerEncoderLayer(
                self.latent_dim,
                num_heads,
                ff_size,
                dropout,
                activation,
                normalize_before,
            )
            encoder_norm = nn.LayerNorm(self.latent_dim)
            self.encoder = SkipTransformerEncoder(encoder_layer,
                                                  num_layers, encoder_norm)
        else:
            # use torch transformer
            encoder_layer = nn.TransformerEncoderLayer(
                d_model=self.latent_dim,
                nhead=num_heads,
                dim_feedforward=ff_size,
                dropout=dropout,
                activation=activation)
            self.encoder = nn.TransformerEncoder(encoder_layer,
                                                 num_layers=num_layers)

    def forward(self,
                noised_motion,
                in_motion_mask,
                timestep,
                text_embeds,
                condition_mask, 
                motion_embeds=None,
                lengths=None,
                
                **kwargs):
        # 0.  dimension matching
        # noised_motion [latent_dim[0], batch_size, latent_dim] <= [batch_size, latent_dim[0], latent_dim[1]]
        bs = noised_motion.shape[0]
        noised_motion = noised_motion.permute(1, 0, 2)
        # 0. check lengths for no vae (diffusion only)
        # if lengths not in [None, []]:
        motion_in_mask = in_motion_mask

        # time_embedding | text_embedding | frames_source | frames_target
        # 1 * lat_d | max_text * lat_d | max_frames * lat_d | max_frames * lat_d

        # 1. time_embeddingno
        # broadcast to batch dimension in a way that's compatible with ONNX/Core ML
        timesteps = timestep.expand(noised_motion.shape[1]).clone()
        time_emb = self.embed_timestep(timesteps).to(dtype=noised_motion.dtype)
        # make it S first
        # time_emb = self.time_embedding(time_emb).unsqueeze(0)
        if self.condition in ["text", "text_uncond"]:
            # make it seq first
            text_embeds = text_embeds.permute(1, 0, 2)
            if self.text_encoded_dim != self.latent_dim:
                # [1 or 2, bs, latent_dim] <= [1 or 2, bs, text_encoded_dim]
                text_emb_latent = self.emb_proj(text_embeds)
            else:
                text_emb_latent = text_embeds
            if motion_embeds is None:
                # source_motion_zeros = torch.zeros(*noised_motion.shape[:2], 
                #                             self.latent_dim, 
                #                             device=noised_motion.device)
                # aux_fake_mask = torch.zeros(condition_mask.shape[0], 
                #                             noised_motion.shape[0], 
                #                             device=noised_motion.device)
                # condition_mask = torch.cat((condition_mask, aux_fake_mask), 
                #                            1).bool().to(noised_motion.device)
                if self.time_fusion == 'concat':
                    emb_latent = torch.cat((time_emb, text_emb_latent), 0)
                else:
                    emb_latent = torch.cat((text_emb_latent + time_emb), 0)

            elif motion_embeds.shape[0] > 5: 
                # ugly way to tell concat the motion or so
                # first embed to low dim space then concat
                motion_embeds_proj = self.pose_proj_in(motion_embeds)
                emb_latent = torch.cat((text_emb_latent + time_emb,
                                        motion_embeds_proj), 0)
                if self.time_fusion == 'concat':
                    emb_latent = torch.cat((time_emb, text_emb_latent,
                                            motion_embeds_proj), 0)
                else:
                    emb_latent = torch.cat((text_emb_latent + time_emb,
                                            motion_embeds_proj), 0)

            else:
                if motion_embeds.shape[-1] != self.latent_dim:
                    motion_embeds_proj = self.pose_proj_in(motion_embeds)
                else:
                    motion_embeds_proj = motion_embeds
                if self.time_fusion == 'concat':
                    emb_latent = torch.cat((time_emb, text_emb_latent,
                                            motion_embeds_proj), 0)
                else:
                    emb_latent = torch.cat((text_emb_latent + time_emb,
                                            motion_embeds_proj), 0)

        else:
            raise TypeError(f"condition type {self.condition} not supported")
        # 4. transformer
        if self.arch == "trans_enc":
            # if self.diffusion_only:
            proj_noised_motion = self.pose_proj_in(noised_motion)
            xseq = torch.cat((emb_latent, proj_noised_motion), axis=0)
            # else:
            # xseq = torch.cat((sample, emb_latent), axis=0)

            # if self.ablation_skip_connection:
            #     xseq = self.query_pos(xseq)
            #     tokens = self.encoder(xseq)
            # else:
            #     # adding the timestep embed
            #     # [seqlen+1, bs, d]
            #     # todo change to query_pos_decoder
            xseq = self.query_pos(xseq)
            if self.time_fusion == 'concat':
                time_token_mask = torch.ones((bs, time_emb.shape[0]),
                                            dtype=bool, device=xseq.device)
                aug_mask = torch.cat((time_token_mask,
                                      condition_mask[:, text_emb_latent.shape[0]:],
                                      condition_mask[:,:text_emb_latent.shape[0]],
                                      motion_in_mask), 1)
            else:
                # condition_mask
                aug_mask = torch.cat((condition_mask[:, text_emb_latent.shape[0]:],
                                      condition_mask[:,:text_emb_latent.shape[0]],
                                      motion_in_mask), 1)

            tokens = self.encoder(xseq,
                                  src_key_padding_mask=~aug_mask)
                
            # if self.diffusion_only:
            denoised_motion_proj = tokens[emb_latent.shape[0]:]
            if self.use_deltas:
                # DROPPED
                denoised_first_pose = self.first_pose_proj(denoised_motion_proj[:1])            
                denoised_motion_only = self.pose_proj_out(denoised_motion_proj[1:])
                denoised_motion_only[~motion_in_mask.T[1:]] = 0
                denoised_motion = torch.zeros_like(noised_motion)
                denoised_motion[1:] = denoised_motion_only
                denoised_motion[0] = denoised_first_pose
            else:
                
                if self.pred_delta_motion and motion_embeds is not None:
                    if self.fuse == 'concat':
                        denoised_motion_proj = torch.cat([denoised_motion_proj,
                                                          motion_embeds_proj],
                                                         dim=-1)
                    elif self.fuse == 'add':
                        denoised_motion_proj = denoised_motion_proj + self.feat_comb_coeff*motion_embeds_proj
                    else:
                        denoised_motion_proj = denoised_motion_proj
                denoised_motion = self.pose_proj_out(denoised_motion_proj)
                denoised_motion[~motion_in_mask.T] = 0


            # zero for padded area
            # else:
            #     sample = tokens[:sample.shape[0]]

        else:
            raise TypeError("{self.arch} is not supoorted")

        # 5. [batch_size, latent_dim[0], latent_dim[1]] <= [latent_dim[0], batch_size, latent_dim[1]]
        denoised_motion = denoised_motion.permute(1, 0, 2)

        return denoised_motion

import math
import torch

from torch import Tensor
from torch.optim.optimizer import Optimizer

from typing import List, Optional


class AnyPrecisionSophiaG(Optimizer):
    def __init__(
        self,
        params,
        lr=1e-4,
        betas=(0.965, 0.99),
        rho=0.04,
        weight_decay=1e-1,
        use_kahan_summation=False,
        momentum_dtype=torch.bfloat16,
        hessian_dtype=torch.bfloat16,
        compensation_buffer_dtype=torch.bfloat16,
        maximize: bool = False,
        capturable: bool = False,
    ):
        if not 0.0 <= lr:
            raise ValueError("Invalid learning rate: {}".format(lr))
        if not 0.0 <= betas[0] < 1.0:
            raise ValueError("Invalid beta parameter at index 0: {}".format(betas[0]))
        if not 0.0 <= betas[1] < 1.0:
            raise ValueError("Invalid beta parameter at index 1: {}".format(betas[1]))
        if not 0.0 <= rho:
            raise ValueError("Invalid rho parameter: {}".format(rho))
        if not 0.0 <= weight_decay:
            raise ValueError("Invalid weight_decay value: {}".format(weight_decay))

        defaults = dict(
            lr=lr,
            betas=betas,
            rho=rho,
            weight_decay=weight_decay,
            use_kahan_summation=use_kahan_summation,
            momentum_dtype=momentum_dtype,
            hessian_dtype=hessian_dtype,
            compensation_buffer_dtype=compensation_buffer_dtype,
            maximize=maximize,
            capturable=capturable,
        )
        super(AnyPrecisionSophiaG, self).__init__(params, defaults)


    def __setstate__(self, state):
        super().__setstate__(state)
    
        for group in self.param_groups:
            group.setdefault('maximize', False)
            group.setdefault('capturable', False)

        state_values = list(self.state.values())
        step_is_tensor = (len(state_values) != 0) and torch.is_tensor(state_values[0]['step'])

        if not step_is_tensor:
            for s in state_values:
                s['step'] = torch.tensor(float(s['step']))
    

    @torch.no_grad()
    def update_hessian(self):
        for group in self.param_groups:
            beta2 = group['betas'][1]
            hessian_dtype = group['hessian_dtype']

            for p in group['params']:
                if p.grad is None:
                    continue

                state = self.state[p]

                if len(state) == 0:
                    state['step'] = torch.zeros((1,), dtype=torch.float, device=p.device) \
                        if self.defaults['capturable'] else torch.tensor(0.)

                    state['exp_avg'] = torch.zeros_like(p, dtype=group['momentum_dtype'], memory_format=torch.preserve_format)
                    state['hessian'] = torch.zeros_like(p, dtype=hessian_dtype, memory_format=torch.preserve_format)
                
                if 'hessian' not in state.keys():
                    state['hessian'] = torch.zeros_like(p, dtype=hessian_dtype, memory_format=torch.preserve_format)

                state['hessian'].mul_(beta2).addcmul_(p.grad, p.grad, value=1 - beta2)


    @torch.no_grad()
    def step(self, closure=None, bs=5120):
        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()

        for group in self.param_groups:
            params_with_grad = []
            grads = []
            exp_avgs = []
            state_steps = []
            hessian = []
            compensations = []
            beta1, beta2 = group['betas']

            for p in group['params']:
                if p.grad is None:
                    continue
                params_with_grad.append(p)
                
                if p.grad.is_sparse:
                    raise RuntimeError('AnyPrecisionSophiaG does not support sparse gradients')
                
                grads.append(p.grad)
                state = self.state[p]

                # State initialization
                if len(state) == 0:
                    state['step'] = torch.zeros((1,), dtype=torch.float, device=p.device) \
                        if self.defaults['capturable'] else torch.tensor(0.)
                    
                    state['exp_avg'] = torch.zeros_like(p, dtype=group['momentum_dtype'], memory_format=torch.preserve_format)
                    state['hessian'] = torch.zeros_like(p, dtype=group['hessian_dtype'], memory_format=torch.preserve_format)
                    
                    if group['use_kahan_summation']:
                        state['compensation'] = torch.zeros_like(p, dtype=group['compensation_buffer_dtype'], memory_format=torch.preserve_format)
                
                if 'hessian' not in state.keys():
                    state['hessian'] = torch.zeros_like(p, dtype=group['hessian_dtype'], memory_format=torch.preserve_format)
                
                if group['use_kahan_summation'] and 'compensation' not in state.keys():
                    state['compensation'] = torch.zeros_like(p, dtype=group['compensation_buffer_dtype'], memory_format=torch.preserve_format)

                exp_avgs.append(state['exp_avg'])
                state_steps.append(state['step'])
                hessian.append(state['hessian'])
                
                if group['use_kahan_summation']:
                    compensations.append(state['compensation'])
                else:
                    compensations.append(None)

                if self.defaults['capturable']:
                    bs = torch.ones((1,), dtype=torch.float, device=p.device) * bs

            sophiag(
                params_with_grad,
                grads,
                exp_avgs,
                hessian,
                compensations,
                state_steps,
                bs=bs,
                beta1=beta1,
                beta2=beta2,
                rho=group['rho'],
                lr=group['lr'],
                weight_decay=group['weight_decay'],
                maximize=group['maximize'],
                capturable=group['capturable'],
                use_kahan_summation=group['use_kahan_summation'],
            )

        return loss


def sophiag(
    params: List[Tensor],
    grads: List[Tensor],
    exp_avgs: List[Tensor],
    hessian: List[Tensor],
    compensations: List[Optional[Tensor]],
    state_steps: List[Tensor],
    capturable: bool = False,
    *,
    bs: int,
    beta1: float,
    beta2: float,
    rho: float,
    lr: float,
    weight_decay: float,
    maximize: bool,
    use_kahan_summation: bool,
):
    if not all(isinstance(t, torch.Tensor) for t in state_steps):
        raise RuntimeError("API has changed, `state_steps` argument must contain a list of singleton tensors")

    func = _single_tensor_sophiag

    func(
        params,
        grads,
        exp_avgs,
        hessian,
        compensations,
        state_steps,
        bs=bs,
        beta1=beta1,
        beta2=beta2,
        rho=rho,
        lr=lr,
        weight_decay=weight_decay,
        maximize=maximize,
        capturable=capturable,
        use_kahan_summation=use_kahan_summation,
    )


def _single_tensor_sophiag(
    params: List[Tensor],
    grads: List[Tensor],
    exp_avgs: List[Tensor],
    hessian: List[Tensor],
    compensations: List[Optional[Tensor]],
    state_steps: List[Tensor],
    *,
    bs: int,
    beta1: float,
    beta2: float,
    rho: float,
    lr: float,
    weight_decay: float,
    maximize: bool,
    capturable: bool,
    use_kahan_summation: bool,
):
    for i, param in enumerate(params):
        grad = grads[i] if not maximize else -grads[i]
        exp_avg = exp_avgs[i]
        hess = hessian[i]
        step_t = state_steps[i]
        compensation = compensations[i]

        if capturable:
            assert param.is_cuda and step_t.is_cuda and bs.is_cuda 

        if torch.is_complex(param):
            grad = torch.view_as_real(grad)
            exp_avg = torch.view_as_real(exp_avg)
            hess = torch.view_as_real(hess)
            param = torch.view_as_real(param)

        # update step
        step_t += 1

        # Perform stepweight decay
        param.mul_(1 - lr * weight_decay)

        # Decay the first and second moment running average coefficient
        exp_avg.mul_(beta1).add_(grad, alpha=1 - beta1)
        
        if capturable:
            step_size = lr 
            step_size_neg = step_size.neg()

            ratio = (exp_avg.abs() / (rho * bs * hess + 1e-15)).clamp(None, 1)
            if use_kahan_summation:
                compensation.addcmul_(exp_avg.sign(), ratio, value=step_size_neg)
                temp_buffer = param.detach().clone()
                param.add_(compensation)
                compensation.add_(temp_buffer.sub_(param.data))
            else:
                param.addcmul_(exp_avg.sign(), ratio, value=step_size_neg)
        else:
            step_size_neg = -lr

            ratio = (exp_avg.abs() / (rho * bs * hess + 1e-15)).clamp(None, 1)
            if use_kahan_summation:
                compensation.addcmul_(exp_avg.sign(), ratio, value=step_size_neg)
                temp_buffer = param.detach().clone()
                param.add_(compensation)
                compensation.add_(temp_buffer.sub_(param.data))
            else:
                param.addcmul_(exp_avg.sign(), ratio, value=step_size_neg)

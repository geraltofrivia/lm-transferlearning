"""
    Pulls an unsupervised fine tuned model from disk, also data, and goes to town on it.

    @TODO: Add embeddings in layer
    @TODO: Check if LR is reset after its fucked up by sltr
"""

# External Lib imports
import os
from functools import partial
from sklearn.utils import class_weight
from typing import List, Union, Callable

os.environ['QT_QPA_PLATFORM'] = 'offscreen'

# FastAI Imports
from fastai import text, lm_rnn

# Torch imports
import torch.nn as nn
import torch.optim as optim

# Mytorch imports
from mytorch.utils.goodies import *
from mytorch import loops, lriters as mtlr

# Local imports
import utils
import main as p2
from data import DataPuller
from options import Phase3 as params, Phase2 as p2params

device = torch.device('cuda')
np.random.seed(42)
torch.manual_seed(42)

'''
    Paths and macros
'''
PATH = Path('resources/proc/imdb')
DUMPPATH = Path('resources/models/runs')
DATA_PROC_PATH = PATH / 'data'
DATA_LM_PATH = PATH / 'datalm'

LM_PATH = Path('resources/models')
LM_PATH.mkdir(exist_ok=True)
PRE_PATH = LM_PATH / 'wt103'
PRE_LM_PATH = PRE_PATH / 'fwd_wt103.h5'
KNOWN_DATASETS = {'imdb': 2, 'trec': 6, 'cornell': 2, 'wikitext': 1}


'''
    Models, Data Samplers etc
'''


class FakeBatchNorm1d(nn.Module):
    """
        Class which keeps its interface same b/w batchnorm1d and doesn't do shit.
        Needed for when I send sliced encoded tensors to classifier to perform pointwise classification.
    """
    def __init__(self):
        super().__init__()

    def forward(self, x):
        return x


class CustomLinearBlock(text.LinearBlock):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bn = FakeBatchNorm1d()


class CustomPoolingLinearClassifier(text.PoolingLinearClassifier):
    """ Overwriting lm_rnn's PoolingLinearClassifier so it uses CustomLinearBlock (with no batchnorm)"""
    def __init__(self, layers, drops):
        super().__init__(layers, drops)
        self.layers = nn.ModuleList([
            CustomLinearBlock(layers[i], layers[i + 1], drops[i]) for i in range(len(layers) - 1)])


class CustomEncoder(lm_rnn.MultiBatchRNN):
    @property
    def layers(self):
        return torch.nn.ModuleList([
            torch.nn.ModuleList([self.encoder, self.encoder_with_dropout]),
            torch.nn.ModuleList([self.rnns[0], self.dropouths[0]]),
            torch.nn.ModuleList([self.rnns[1], self.dropouths[1]]),
            torch.nn.ModuleList([self.rnns[2], self.dropouths[2]])
        ])


class TextClassifier(nn.Module):

    # @TODO: inject comments.
    def __init__(self,
                 _device: torch.device,
                 n_token: int,
                 dps: list,
                 n_classes: List[int],
                 enc_wgts = None,
                 _debug=False):
        super(TextClassifier, self).__init__()
        """
        :param n_token: int representing vocab size
        :param n_classes: list representing multiple classes, each by its number of classes.
            eg. n_classes = [2] -> one task; with 2 classes
            eg. n_classes = [2, 6] -> two tasks, first with 2 classes, and one with 6.
        """

        self.device = _device

        # Load the pre-trained model
        encargs = {'ntoken': n_token, 'emb_sz': 400, 'n_hid': 1150,
                   'n_layers': 3, 'pad_token': 0, 'qrnn': False, 'bptt': 70, 'max_seq': 1400,
                   'dropouti': dps[0], 'wdrop': dps[1], 'dropoute': dps[2], 'dropouth': dps[3]}
        self.encoder = CustomEncoder(**encargs).to(self.device)

        if enc_wgts:
            self.encoder.load_state_dict(enc_wgts)

        '''
            Make multiple classifiers (depending upon n_classes)
            
            
            Explanation:
                400*3 because input is [ h_T, maxpool, meanpool ]
                50 is hidden layer dim
                2 is n_classes

                0.4, 0.1 are drops at various layers
        '''
        self.linear = torch.nn.ModuleList([CustomPoolingLinearClassifier(layers=[400 * 3, 50, cls], drops=[dps[4], 0.1]).to(self.device)
                       for cls in n_classes])
        self.domain_clf = p2.CustomLinear(layers=p2params.domclas_layers + [len(n_classes)], drops=p2params.domclas_drops).to(self.device)
        self.encoder.reset()

    @property
    def layers(self):
        layers = [x for x in self.encoder.layers]
        len_layers = [len(lin.layers) for lin in self.linear] + [len(self.domain_clf.layers)]
        srcs = [lin for lin in self.linear] + [self.domain_clf]
        for i in range(max(len_layers)):
            tmp_layers = []
            for src in range(len(srcs)):
                if len_layers[src] != 0:
                    tmp_layers.append(srcs[src].layers[i])
                    len_layers[src] -= 1
            layers.append(torch.nn.ModuleList(tmp_layers))

        return torch.nn.ModuleList(layers)

    def forward(self, x: torch.tensor, domain: torch.tensor, task_index: dict = None):
        """ x is bs, sl; dom is bs indicating the task.
                task index can reroute tasks of a domain to another.
                Eg. if task_index = {1:0}, all those tasks which are of domain[i] = 1, will not be done with linear[1] but with linear[0]
        """

        if task_index is None:
            task_index = {}

        # Encoding all the data
        x_proc = self.encoder(x.transpose(1, 0))

        sl, bs, _ = x_proc[0][0].shape

        score = []
        for pos, dom in enumerate(domain):
            """
                Right now, x_proc looks like ( [(sl, bs, hdim)*n_layers_enc], [(sl, bs, hdim)*n_layers_enc)] 
                    for dropped and non dropped outputs respectively.
                
                Depending on {dom.item()}^th task on {i}^th position,
                We slice from x_proc a tensor of (sl, 1, hdim) shape based on i, and feed it to the {dom}'th decoder.
            
                Finally, we concat the outputs in a nice little list and pretend nothing happened [:
                
                NOTE: This shit might be slow                     
            """
            x_proc_pos = ([layer_op[:, pos].view(sl, 1, -1) for layer_op in x_proc[0]],
                         [layer_op[:, pos].view(sl, 1, -1) for layer_op in x_proc[1]])
            score.append(self.linear[dom.item() if dom.item() not in task_index else task_index[dom.item()]](x_proc_pos)[0])

        return score, x_proc

    def domain(self, x_proc):
        # @TODO: FIX
        # print(x_proc)
        x_proc = list(x_proc)
        x_proc[1] = [GradReverse.apply(enc_tensr) for enc_tensr in x_proc[1]]
        return self.domain_clf(x_proc)[0]

    def predict(self, x, d, task_index: None):
        with torch.no_grad():
            self.eval()
            predicted = self.forward(x, d, task_index)
            self.train()
            return predicted


def epoch_end_hook(lr_schedule: mtlr.LearningRateScheduler) -> None:
    """
        Calling the lr schedule to unfreeze one layer and unfreezing one layer.

    :param lr_schedule: the schedule we want to reset.
    :return: Nada
    """

    lr_schedule.unfreeze()
    lr_schedule.reset()


def custom_argmax(x: List[torch.Tensor], dim: int = 1) -> torch.Tensor:
    """ Expects a list of tensors, and computes individual's argmax"""
    return torch.cat([pred.argmax(dim=dim) for pred in x])


# noinspection PyUnresolvedReferences
def _list_eval(y_pred: list, y_true: torch.Tensor, tasks: int = 1, task_index: torch.Tensor = None) -> List[np.float]:
    """
        Expects y_pred to be a list of tensors, but y_true to be a one tensor.
        Also takes tasks as inputs and another tensor which specifies which example belongs to which task

        Returns a list of floats (one for each task. Minimum: 1)

        :param y_pred: list of n_batches items each of shape (1, nc_t) where nc_t can have multiple values
        :param y_true: tensor of shape (b, 1)
        :param tasks: (int, optional) a number of unique tasks for which we report eval
        :param task_index: (torch.Tensor, optional) a vector indicating which tasks
    """
    acc = (custom_argmax(y_pred, dim=1) == y_true).float()
    if not tasks > 1 or task_index is None:
        return torch.mean(acc).item()

    return [torch.mean(torch.masked_select(acc, task_index == task)).item() for task in range(tasks)]


# noinspection PyUnresolvedReferences
def _eval(y_pred, y_true, **args):
    """
        Expects a batch of input

        Ignores a bunch of extra args.

        :param y_pred: tensor of shape (b, nc)
        :param y_true: tensor of shape (b, 1)
    """
    # print(y_pred[0])
    return torch.mean((torch.argmax(y_pred, dim=1) == y_true).float())


# noinspection PyUnresolvedReferences
def multitask_classification_loss(y_pred: list, y_true: torch.Tensor, loss_fn: List[Union[torch.nn.Module, Callable]],
                                  task_index: torch.Tensor = None, ignore_dataset: list = [], **args) -> torch.Tensor:
    """
        Accepts different sized y_preds where each element can have [1, _] shapes.
        Provide one or multiple loss functions depending upon the num of tasks, using our regular -partial- thing.

        Eg. lfn = partial(multitask_classification_loss, loss_fn:torch.nn.CrossEntropyLoss())

    :param y_pred: (list) of tensors where each tensor is of shape (1, _) of length bs
    :param y_true: (torch.Tensor) of shape (bs,)
    :param loss_fn: (torch.nn.Module or a function; or a list of those) which calculate the loss given a y_true and y_pred.
    :param task_index: (torch.Tensor, Optional) of shape (bs,) which dictates which loss to use.
                       Must be provided if there are multiple loss_fns provided
    :param ignore_dataset: (list of ints) indicating which task_index values to ignore.
            Eg. if task_index[0] -> 1 implies that its from 'imdb' task, and you won't wanna train on it, simply pass [1] as ignore_dataset
    :return: the loss value (torch.Tensor)
    """

    # Case 1, only one task -> len(loss_fn) == 1. Ignore task index, in this case
    if len(loss_fn) == 1:
        losses = torch.cat([loss_fn[0](_y_pred.view(1, -1), y_true[i].unsqueeze(0)).view(-1)
                            for i, _y_pred in enumerate(y_pred)])

    else:
        # Case 2: multiple loss functions. In that case, choose the loss fn based on task index
        assert len(y_pred) == y_true.shape[0] == task_index.shape[0], f"Mismatch between y_pred of {len(y_pred)} items, " \
            f"y_true of len {y_true.shape[0]}, and task_index of len {task_index.shape[0]}"

        losses = [loss_fn[task_index[i].item()](_y_pred.view(1, -1), y_true[i].unsqueeze(0)).view(-1)
                  for i, _y_pred in enumerate(y_pred) if task_index[i].item() not in ignore_dataset]

        if len(losses) == 0:
            # Edge case: all the entries are to be ignored
            losses = torch.tensor(0, device=task_index.device, dtype=torch.float)
        else:
            losses = torch.cat(losses)

    return torch.sum(losses)


def domain_classifier_loss(y_pred: list, y_true: torch.Tensor, loss_fn: List[Union[torch.nn.Module, Callable]], **args):
    """ Thin wrapper over loss fn to accept misguided args."""
    return loss_fn(y_pred, y_true)


if __name__ == "__main__":

    # Get args from console
    ap = argparse.ArgumentParser()
    ap.add_argument("-q", "--quick", type=str2bool, required=False, default=False,
                    help="True if you want to only train on first 1000 train,test samples")
    ap.add_argument("--debug", type=str2bool, required=False, default=True,
                    help="True if you want a verbose run")
    ap.add_argument("-p", "--pretrained", type=str2bool, required=False, default=True,
                    help="True if you want a verbose run")
    ap.add_argument("-sf", "--safemode", type=str2bool, required=False,
                    help="True if you dont want to save anything")
    ap.add_argument("-m", "--message", type=str, required=False, default='Default Message',
                    help="Message to be saved alongwith traces")
    ap.add_argument("-ms", "--modelsuffix", default='', type=str, required=False,
                    help="Input either `_lowaux`;`_hightrn`;`_final` or nothing depending on which kind of model you want to load.")
    ap.add_argument("-mn", "--modelnum", required=True,
                    help="Need to provide the folder name (not the entire dir) to the desired phase 2 model. E.g. `--modeldir 2` shall suffice.")
    ap.add_argument("-md", "--modeldir", required=False,
                    help="Overwrites where to get the data from.")
    ap.add_argument("-d", "--datasets", type=str, required=True,
                    help="Comma separated two dataset names like wikitext,imdb")
    ap.add_argument("-l", "--lambda", type=float, required=False,
                        help="Desired value of loss scale factor for dann module")
    ap.add_argument("-z", "--zeroshot", type=str, required=False,
                    help="Datasets on which we ought to ignore shit")

    args = vars(ap.parse_args())

    QUICK = args['quick']
    DEBUG = args['debug']
    MODEL_NUM = args['modelnum']
    PRETRAINED = args['pretrained']
    MODEL_SUFFIX = args['modelsuffix']
    MODEL_DIR = args['modeldir']
    SAFE_MODE = args['safemode']
    MESSAGE = args['message']
    DATASETS = args['datasets'].split(',')
    LOSS_SCALE = args['lambda']
    ZERO = args['zeroshot'].split(',') if args['zeroshot'] is not None else None

    if ZERO is not None:

        assert set(DATASETS).issuperset(
            set(ZERO)), f'At least one of the dataset which you instructed to ignore: {ZERO} is not being considered: {DATASETS}'

        ZERO = [DATASETS.index(d) for d in ZERO]
        if ZERO == 0:
            # If the task which we want to leave untrained in task 0,
            alter_task = 1
        else:
            alter_task = 0

        ZERO_TASK_INDEX = {ZERO[0]: alter_task}

    else:
        ZERO_TASK_INDEX = None

    if MODEL_DIR is None:
        UNSUP_MODEL_DIR = DUMPPATH / '_'.join(DATASETS) / str(MODEL_NUM)
    else:
        UNSUP_MODEL_DIR = DUMPPATH / MODEL_DIR / str(MODEL_NUM)

    assert MODEL_SUFFIX in ['_lowaux', '_hightrn', '', '_final'], 'Incorrect Suffix given with which to load model'

    params.quick = QUICK
    params.model_dir = str(UNSUP_MODEL_DIR) + ' and ' + str(MODEL_NUM)
    params.model_suffix = MODEL_SUFFIX
    params.datasets = DATASETS
    if LOSS_SCALE is not None:
        params.loss_scale = LOSS_SCALE

    # Create representations of text using old itos
    itos_path = UNSUP_MODEL_DIR / 'itos.pkl'
    itos2 = pickle.load(itos_path.open('rb'))
    stoi2 = {v: k for k, v in enumerate(itos2)}

    data_puller = DataPuller(debug=False, max_vocab=params.max_vocab_task, min_freq=params.min_vocab_freq,
                             trim_trn=1000, trim_val=1000)

    trn_texts, trn_labels, val_texts, val_labels, task_specific_weights = [], [], [], [], []
    for dataset in DATASETS:

        trn_texts_, trn_labels_, val_texts_, val_labels_, itos = data_puller.get(dataset, supervised=True,
                                                                                 merge_vocab=params.max_vocab_others,
                                                                                 trim=params.quick, cached=True)

        # Lose label 2 from imdb
        if dataset == 'imdb':
            trn_texts_ = trn_texts_[trn_labels_ < 2]
            trn_labels_ = trn_labels_[trn_labels_ < 2]

        # If dataset is the one to be ignored:
        if ZERO and dataset == DATASETS[ZERO[0]]:

            # Crop substantial part of the data away
            trn_texts_ = trn_texts_[: min(len(trn_texts_), 2000)]
            trn_labels_ = trn_labels_[: min(len(trn_texts_), 2000)]

            val_texts_ = val_texts_[: min(len(trn_texts_), 2000)]
            val_labels_ = val_labels_[: min(len(trn_texts_), 2000)]

        # Compute weights for cross entropy loss
        class_weights_ = class_weight.compute_class_weight('balanced', classes=range(KNOWN_DATASETS[dataset]), y=trn_labels_)

        # Store all things in a nice format
        trn_texts.append(trn_texts_)
        trn_labels.append(trn_labels_)
        val_texts.append(val_texts_)
        val_labels.append(val_labels_)
        task_specific_weights.append(class_weights_)

    # At this point, the five lists contain each some aspect of our datasets. itos (the list overwritten in the loop) contains the vocab.

    # Transform words from data_puller.itos vocabulary to that of the pretrained model (__main__.itos2)
    _itos2 = dict(enumerate(itos2))
    for i, (trn_texts_, val_texts_) in enumerate(zip(trn_texts, val_texts)):
        trn_texts[i] = [[stoi2[_itos2.get(i, '_unk_')] for i in sent] for sent in trn_texts_]
        val_texts[i] = [[stoi2[_itos2.get(i, '_unk_')] for i in sent] for sent in val_texts_]

    # Compute dataset specific weights. Formula: n_samples / (n_classes * np.bincount(<flatlist_indexing_all_samples_for_all_datasets>))
    bincount = np.array([len(trn_labels_) for trn_labels_ in trn_labels])
    dataset_specific_weights = np.sum(bincount) / (len(bincount) * bincount)


    '''
        Make model
    '''
    dps = list(params.encoder_dropouts)
    enc_wgts = torch.load(UNSUP_MODEL_DIR / ('unsup_model_enc' + MODEL_SUFFIX + '.torch'), map_location=lambda storage, loc: storage)
    n_classes = [KNOWN_DATASETS[d] for d in DATASETS]
    clf = TextClassifier(device, len(itos2), dps, enc_wgts=enc_wgts if PRETRAINED else None, n_classes=n_classes)

    '''
        Setup things for training (data, loss, opt, lr schedule etc
    '''
    bs = params.bs
    loss_fns = [torch.nn.CrossEntropyLoss(weight=torch.tensor(w, device=device, dtype=torch.float))
                for w in task_specific_weights]
    loss_main_fn = partial(multitask_classification_loss, loss_fn=loss_fns, ignore_dataset=ZERO if ZERO is not None else [])
    if len(DATASETS) > 1:
        loss_aux_fn = partial(domain_classifier_loss, loss_fn=torch.nn.CrossEntropyLoss(
            torch.tensor(dataset_specific_weights, device=device, dtype=torch.float)))
    else:
        # Weights dont make sense if only one domain is being worked with
        loss_aux_fn = partial(domain_classifier_loss, loss_fn=torch.nn.CrossEntropyLoss())
    opt_fn = partial(optim.Adam, betas=params.adam_betas)
    opt = make_opt(clf, opt_fn, lr=params.lr.init)

    # Make data
    data_fn = partial(utils.DomainAgnosticSortishSampler, _batchsize=bs, _padidx=1)
    data_train = [{'x': trn_texts_, 'y': trn_labels_} for trn_texts_, trn_labels_ in zip(trn_texts, trn_labels)]
    data_valid = [{'x': val_texts_, 'y': val_labels_} for val_texts_, val_labels_ in zip(val_texts, val_labels)]
    data = {'train': data_train, 'valid': data_valid}

    # Make lr scheduler
    org_iterations = len(data_fn(data_train))
    freeze_mask = np.array([0 for _ in opt.param_groups])
    freeze_mask[-1] = 1
    lr_args = {'iterations': org_iterations, 'cycles': 1}
    lr_schedule = mtlr.LearningRateScheduler(optimizer=opt, lr_args=lr_args, lr_iterator=mtlr.CosineAnnealingLR, freeze_mask=freeze_mask)

    save_args = {'torch_stuff': [tosave('model.torch', clf.state_dict()), tosave('model_enc.torch', clf.encoder.state_dict())]}
    save_fnames = {'torch_stuff':
                       {'hightrn':
                            {'model': 'sup_model_hightrn.torch',
                             'enc': 'sup_model_hightrn_enc.torch'},
                        'lowaux':
                            {'model': 'sup_model_lowaux.torch',
                             'enc': 'sup_model_lowaux_enc.torch'}}}

    args = {'epochs': 1, 'epoch_count': 0, 'data': data, 'device': device, 'opt': opt,
            'loss_main_fn': loss_main_fn, 'loss_aux_fn': loss_aux_fn, 'model': clf,
            'train_fn': clf, 'predict_fn': partial(clf.predict, task_index=ZERO_TASK_INDEX), 'train_aux_fn': clf.domain,
            'epoch_end_hook': partial(epoch_end_hook, lr_schedule=lr_schedule),
            'weight_decay': params.weight_decay, 'clip_grads_at': params.clip_grads_at, 'lr_schedule': lr_schedule,
            'loss_aux_scale': params.loss_scale if len(DATASETS) > 1 else 0, 'tasks': len(DATASETS),
            'data_fn': data_fn, 'eval_fn': _list_eval, 'eval_aux_fn': _eval,
            'save': not SAFE_MODE, 'save_params': params, 'save_dir': UNSUP_MODEL_DIR, 'save_fnames': save_fnames}

    '''
        Training schedule: 
        NOTE: Removed all freezing
        
        1. Unfreeze one layer. Train for 1 epoch
        2 - 5. Unfreeze one layer, train for 1 epoch
        3. Train for 15 epochs (after all layers are unfrozen). Use 15 cycles for cosine annealing.
        
        @TODO: save_above_trn, save_below_aux needs to be fixed to handle multiple values!!
        
    '''
    # Freeze all layers
    traces = utils.dann_loop(**args)

    args['save_above_trn'] = np.max(traces[utils.TRACES_FORMAT['train_acc_main']])
    args['epoch_count'] += 1
    traces_new = utils.dann_loop(**args)
    traces = [a+b for a, b in zip(traces, traces_new)]

    args['save_above_trn'] = np.max(traces[utils.TRACES_FORMAT['train_acc_main']])
    args['epoch_count'] += 1
    traces_new = utils.dann_loop(**args)
    traces = [a+b for a, b in zip(traces, traces_new)]

    args['save_above_trn'] = np.max(traces[utils.TRACES_FORMAT['train_acc_main']])
    args['save_above_aux'] = np.min(traces[utils.TRACES_FORMAT['train_acc_aux']][2:])
    args['epoch_count'] += 1
    traces_new = utils.dann_loop(**args)
    traces = [a+b for a, b in zip(traces, traces_new)]

    args['save_above_trn'] = np.max(traces[utils.TRACES_FORMAT['train_acc_main']])
    args['save_above_aux'] = np.min(traces[utils.TRACES_FORMAT['train_acc_aux']][2:])
    args['epoch_count'] += 1
    traces_new = utils.dann_loop(**args)
    traces = [a+b for a, b in zip(traces, traces_new)]

    args['epochs'] = 6
    args['save_above_trn'] = np.max(traces[utils.TRACES_FORMAT['train_acc_main']])
    args['save_above_aux'] = np.min(traces[utils.TRACES_FORMAT['train_acc_aux']][2:])
    args['epoch_count'] += 1
    args['notify'] = True

    traces_new = utils.dann_loop(**args)
    traces = [a+b for a, b in zip(traces, traces_new)]

    if not SAFE_MODE:
        mt_save(UNSUP_MODEL_DIR, message=MESSAGE, message_fname="message_p3.txt",
                torch_stuff=[tosave('sup_model_final.torch', clf.state_dict())],
                pickle_stuff=[tosave('final_sup_traces.pkl', traces), tosave('unsup_options.pkl', params)])

    def understand_traces(traces):
        # Simply find places where each of trn acc is highest
        trn_ids = np.argmax(np.array(traces[utils.TRACES_FORMAT['train_acc_main']]), axis=0)
        ids = list(np.unique(trn_ids))

        # Find where vals is highest
        val_ids = np.argmax(np.array(traces[utils.TRACES_FORMAT['val_acc']]), axis=0)
        ids.extend(list(np.unique(val_ids)))

        # Find where dom is lowest
        dom_ids = np.argmin(np.array(traces[utils.TRACES_FORMAT['train_acc_aux']][3:]), axis=0) + 3
        ids.extend(list(np.unique(dom_ids)))

        _ids = ids[0]
        # Now to print these things
        for _ids in ids:
            print(f'@{_ids:3d}: ',
                  np.around(traces[utils.TRACES_FORMAT['train_acc_main']][_ids], decimals=4),
                  '|', np.around(traces[utils.TRACES_FORMAT['val_acc']][_ids], decimals=4),
                  '|', np.around(traces[utils.TRACES_FORMAT['train_acc_aux']][_ids], decimals=4))

    understand_traces(traces)
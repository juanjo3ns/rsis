import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from args import get_parser
from pycocotools.cocoeval import COCOeval
from pycocotools.coco import COCO
import pycocotools.mask as mask
from utils.utils import batch_to_var, make_dir, outs_perms_to_cpu, load_checkpoint, check_parallel
from scipy.ndimage.measurements import center_of_mass
from scipy.ndimage.morphology import binary_fill_holes
from modules.model import RIASS, FeatureExtractor
from test import test
from PIL import Image
from scipy.misc import imread
from dataloader.dataset_utils import get_dataset
import torch
import numpy as np
from torch.autograd import Variable
from skimage import measure
from collections import Counter
from torchvision import transforms
import torch.utils.data as data
import pickle
import sys, os
import json
from scipy.ndimage.interpolation import zoom
import random
from dataloader.dataset_utils import pascal_palette, sequence_palette


def display_masks(anns, colors, im_height=448, im_width=448, no_display_text=False, display_route=False):
    """Display annotations in image."""

    if len(anns) == 0:
        return 0
    ax = plt.gca()
    box_width = 30
    box_height = 10
    ax.set_autoscale_on(False)

    xdata = []
    ydata = []

    for i, ann in enumerate(anns):

        if display_route:
            display_txt = "%d" % (i)
        else:
            display_txt = "%d: %s. %.2f"%(i, ann['category_name'],ann['score'])

        if 'ignore' in ann:
            if ann['ignore']==1:
                continue
        #display_txt = "%d: %s. %.2f"%(i, ann['category_name'],ann['score'])
        display_txt = ann['category_name']
        if display_txt == 'motorbike':
            display_txt = 'motor'
        elif display_txt == 'bicycle':
            display_txt = 'bike'
        elif display_txt == 'dining table':
            display_txt = 'table'
        elif display_txt == 'potted plant':
            display_txt = 'plant'
        elif display_txt == 'airplane':
            display_txt = 'plane'
        if type(ann['segmentation']['counts']) == list:
            rle = mask.frPyObjects([ann['segmentation']],
                                         im_height, im_width)
        else:
            rle = [ann['segmentation']]
        m = mask.decode(rle)
        y,x = center_of_mass(m.squeeze())
        x = max(0,x-box_width)
        y = max(0,y-box_height)
        y = min(m.shape[0] - box_height, y)
        x = min(m.shape[1] - box_width, x)

        xdata.append(x)
        ydata.append(y)

        img = np.ones((m.shape[0], m.shape[1], 3))
        color_mask = np.array(colors[i])/255.0
        for i in range(3):
            img[:,:,i] = color_mask[i]
        ax.imshow(np.dstack( (img, m*0.5) ))
        if not no_display_text:

            ax.text(x, y, display_txt,
                    bbox = {'facecolor':color_mask, 'alpha':0.6})

    xdata = np.array(xdata)
    ydata = np.array(ydata)

    if display_route:
        line = matplotlib.lines.Line2D(xdata, ydata, color='r', linewidth=1)
        ax = plt.subplot(111)
        ax.add_line(line)

def resize_mask(args, pred_mask,height,width,flip=False,ignore_pixels = None):
    """
    Processes the mask for evaluation.
    Args:
        args: project arguments
        pred_mask: the mask that has been predicted
        height, width to resize the predicted mask into
    Returns:
        segmentation: the resized and processed mask
        is_valid: bool to determine if we use this mask or not
    """
    # load masks to get original size
    is_valid = True
    pred_mask = zoom(pred_mask.reshape([pred_mask.shape[0],
                                        pred_mask.shape[1], 1]),
                                        [float(height)/pred_mask.shape[0],
                                        float(width)/pred_mask.shape[1],1],
                                        order=1)
    #pred_mask = resize(pred_mask.reshape([args.D, args.D, 1]), height, width)
    #th_ = (np.max(pred_mask) - np.min(pred_mask))/2
    #th = min(args.mask_th,th_)
    th = args.mask_th

    segmentation = (pred_mask > th).astype("uint8")
    #segmentation = binary_fill_holes(segmentation).astype("uint8")
    if ignore_pixels is not None:
        segmentation[ignore_pixels==1] = 0
    if np.sum(segmentation) < args.min_size*height*width:
        is_valid = False
    if args.keep_biggest_blob and is_valid:
        # detect connected components in mask
        labeled_blobs = measure.label(segmentation,background=0).flatten()
        # find the biggest one
        count = Counter(labeled_blobs)
        s = []
        max_num = 0
        for v,k in count.iteritems():
            if v == 0:
                continue
            if k > max_num:
                max_num = k
                max_label = v
        # build mask from the largest connected component
        segmentation = (labeled_blobs == max_label).astype("uint8")
    if flip:
        segmentation = np.flip(segmentation,axis=1)

    segmentation = mask.encode(np.asfortranarray(segmentation.reshape([height,width,1])))[0]
    segmentation_raw = (pred_mask > th).astype("uint8")
    #segmentation_raw = binary_fill_holes(segmentation_raw).astype("uint8")
    segmentation_raw = mask.encode(np.asfortranarray(segmentation_raw.reshape([height,width,1])))[0]
    return segmentation, is_valid, segmentation_raw

def create_annotation(args, imname, pred_mask, class_id, score, classes, is_valid = True):
    """Creates annotation object following the COCO API ground truth format"""

    ann = dict()
    ann['image_id'] = imname
    ann['category_id'] = class_id
    ann['category_name'] = classes[class_id]

    # if the mask is all 0s after thresholding we don't use it either
    ann['segmentation'] = pred_mask
    ann['score'] = score
    if is_valid:
        return ann
    else:
        return None


def create_coco_object(args,image_names,classes):
    """
    Initialize the coco object where annotations will be added for evaluation
    """
    coco = dict()
    ann_file = os.path.join(args.pascal_dir,'pascal_%s'%(args.eval_split)+'.json')
    categories_list = list()
    for i,cat in enumerate(classes[1:]):
        actual_cat = dict()
        actual_cat['id'] = i+1
        categories_list.append(actual_cat)
    coco['categories'] = categories_list
    image_list = list()
    for im in image_names:
        actual_image = dict()
        actual_image['height'] = 300
        actual_image['width'] = 300
        actual_image['id'] = im
        image_list.append(actual_image)
    coco['images'] = image_list
    coco['annotations'] = list()
    with open(ann_file,'w') as fp:
        json.dump(coco,fp)
    coco = COCO(ann_file)

    return coco


class Evaluate():

    def __init__(self,args):

        self.split = args.eval_split
        self.display = args.display
        self.no_display_text = args.no_display_text
        self.dataset = args.dataset
        self.all_classes = args.all_classes
        self.save_gates = args.save_gates
        self.average_gates = args.average_gates
        self.use_cats = args.use_cats
        if hasattr(args, 'rnn_type'):
            self.rnn_type = args.rnn_type
        else:
            self.rnn_type = 'lstm'
        to_tensor = transforms.ToTensor()
        normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                         std=[0.229, 0.224, 0.225])


        image_transforms = transforms.Compose([to_tensor,normalize])

        dataset = get_dataset(args, self.split, image_transforms,augment=False, imsize=args.imsize)

        self.sample_list = dataset.get_sample_list()
        self.class_names = dataset.get_classes()
        print args.pascal_dir
        if args.dataset =='pascal':
            self.gt_file = pickle.load(open(os.path.join(args.pascal_dir,'VOCGT_%s.pkl'%(self.split)),'rb'))
            self.key_to_anns = dict()
            self.ignoremasks = {}
            for ann in self.gt_file:
                if ann['ignore'] == 1:
                    if type(ann['segmentation']['counts']) == list:
                        im_height = ann['segmentation']['size'][0]
                        im_width = ann['segmentation']['size'][1]
                        rle = mask.frPyObjects([ann['segmentation']],
                                                     im_height, im_width)
                    else:
                        rle = [ann['segmentation']]
                    m = mask.decode(rle)
                    self.ignoremasks[ann['image_id']] = m
                if ann['image_id'] in self.key_to_anns.keys():
                    self.key_to_anns[ann['image_id']].append(ann)
                else:
                    self.key_to_anns[ann['image_id']]=[ann]
            self.coco = create_coco_object(args,self.sample_list,self.class_names)

        elif args.dataset =='coco':
            root = os.path.join(args.coco_dir,'images')
            annFile = '%s/annotations/instances_%s2014.json'%(args.coco_dir,self.split)
            self.coco = COCO(annFile)

        self.loader = data.DataLoader(dataset,batch_size=args.batch_size,
                                             shuffle=False,
                                             num_workers=args.num_workers,
                                             drop_last=False)

        self.args = args
        self.flip_in_eval = args.flip_in_eval
        self.colors = []
        palette = sequence_palette()
        inv_palette = {}
        for k,v in palette.iteritems():
            inv_palette[v] = k
        num_colors = len(inv_palette.keys())
        for i in range(num_colors):
            if i == 0 or i == 21:
                continue
            c = inv_palette[i]
            self.colors.append(c)

        encoder_dict, decoder_dict, _, _, load_args = load_checkpoint(args.model_name)
        load_args.nconvlstm = 5
        #load_args.limit_width = True
        self.args.use_feedback = load_args.use_feedback
        #load_args.base_model = args.base_model
        self.encoder = FeatureExtractor(load_args)
        self.decoder = RIASS(load_args)

        print(load_args)

        if args.ngpus > 1 and args.use_gpu:
            self.decoder = torch.nn.DataParallel(self.decoder,device_ids=range(args.ngpus))
            self.encoder = torch.nn.DataParallel(self.encoder,device_ids=range(args.ngpus))

        encoder_dict, decoder_dict = check_parallel(encoder_dict,decoder_dict)
        self.encoder.load_state_dict(encoder_dict)
        self.decoder.load_state_dict(decoder_dict)

        if args.use_gpu:
            self.encoder.cuda()
            self.decoder.cuda()

        self.encoder.eval()
        self.decoder.eval()

    def _create_json(self):

        predictions = list()
        if self.save_gates:
            sample2gates = {}
        argmax_preds = {}
        acc_samples = 0
        print "Creating annotations..."

        for batch_idx, (inputs, targets) in enumerate(self.loader):
            if self.flip_in_eval:
                inputs = np.flip(inputs.numpy(),axis=-1).copy()
                inputs = torch.from_numpy(inputs)
            x, y_mask, y_class, sw_mask, sw_class = batch_to_var(self.args, inputs, targets)
            num_objects = np.sum(sw_mask.data.float().cpu().numpy(),axis=-1)
            if self.save_gates:

                outs, true_perms, stop_probs, gates =  test(self.args,
                                                            self.encoder,
                                                            self.decoder, x, y_mask,
                                                            y_class, sw_mask,
                                                            sw_class,
                                                            save_gates = self.save_gates,
                                                            average = self.average_gates)
            else:
                outs, true_perms, stop_probs =  test(self.args, self.encoder,
                                                    self.decoder, x, y_mask,
                                                    y_class, sw_mask,
                                                    sw_class)
            out_scores = outs[1]
            out_scores = out_scores.cpu().numpy()
            stop_scores = stop_probs.cpu().numpy()

            w = x.size()[-1]
            h = x.size()[-2]
            out_masks, out_classes, y_mask, y_class, prev_masks = outs_perms_to_cpu(self.args,outs,true_perms,h,w)
            if self.args.use_gt_cats:
                out_classes = y_class[:,0:self.args.maxseqlen]

            if self.args.use_gt_masks:
                out_masks = y_mask[:,0:self.args.maxseqlen]
                #out_classes = np.argmax(out_scores,axis=-1)
            for s in range(out_masks.shape[0]):
                this_pred = list()
                sample_idx = self.sample_list[s+acc_samples]

                if self.args.dataset == 'pascal':
                    ignore_mask = self.ignoremasks[sample_idx]
                else:
                    ignore_mask = None

                if self.save_gates:
                    if self.rnn_type == 'lstm':
                        sample2gates[sample_idx] = {'hidden':{},'cell':{},'forget':{},'input':{}}
                    else:
                        sample2gates[sample_idx] = {'hidden':{},'update':{},'reset':{}}
                    if not self.average_gates:
                        sample2gates[sample_idx] = {'hidden':{}}
                if self.dataset == 'pascal':
                    image_dir = os.path.join(args.pascal_dir,'JPEGImages',sample_idx +'.jpg')
                elif self.dataset == 'coco':
                    path = self.coco.loadImgs(sample_idx)[0]['file_name']
                    image_dir = os.path.join(args.coco_dir,'images',self.split+'2014',path)
                elif self.dataset == 'cityscapes':
                    sample_idx = sample_idx.split('.')[0]
                    image_dir = sample_idx + '.png'
                elif self.dataset == 'leaves':
                    image_dir = sample_idx

                im = imread(image_dir)
                if self.flip_in_eval:
                    im = np.flip(im,axis=1)
                h = im.shape[0]
                w = im.shape[1]
                objectness_scores = []
                class_scores = []
                reached_end = False
                for i in range(out_masks.shape[1]):

                    if reached_end:
                        break
                    objectness = stop_scores[s][i][0]
                    if objectness < args.stop_th:
                        #reached_end = True
                        continue
                    pred_mask = out_masks[s][i]
                    # store class with max confidence for display
                    if args.class_th == 0.0:
                        max_class = 1
                    else:
                        max_class = out_classes[s][i]
                    # process mask to create annotation

                    pred_mask, is_valid,raw_pred_mask = resize_mask(args,pred_mask,h,w,self.flip_in_eval,ignore_mask)

                    # for evaluation we repeat the mask with all its class probs
                    for cls_id in range(len(self.class_names)):
                        if cls_id == 0:
                            # ignore eos
                            continue

                        if not self.use_cats:
                            if not cls_id == max_class:
                                continue
                        if not self.args.use_gt_cats:
                            pred_class_score = out_scores[s][i][cls_id]
                            pred_class_score_mod = pred_class_score*objectness
                        else:
                            if not cls_id == max_class:
                                continue
                            else:
                                pred_class_score = 1

                        if self.args.use_gt_stop:
                            if y_class[s][i] == 0:
                                continue
                        '''
                        if not cls_id == max_class:
                            continue
                        '''
                        '''
                        #uncomment to filter out preds based on score
                        if pred_class_score < args.class_th:
                            continue
                        '''

                        ann = create_annotation(self.args, sample_idx, pred_mask,
                                                cls_id, pred_class_score_mod,
                                                self.class_names,is_valid)
                        if ann is not None:
                            if self.dataset == 'leaves':
                                if objectness > args.class_th:
                                    this_pred.append(ann)

                                    if self.save_gates:
                                        for key in gates.keys():
                                            for i_g in gates[key].keys():
                                                value = gates[key][i_g][i, s, :, :].squeeze()
                                                if i_g not in sample2gates[sample_idx][key].keys():
                                                    sample2gates[sample_idx][key][i_g] = []
                                                sample2gates[sample_idx][key][i_g].append(value)
                            else:
                                # for display we only take the mask with max confidence
                                if cls_id == max_class and pred_class_score_mod >= self.args.class_th:
                                    ann_save = create_annotation(self.args, sample_idx, raw_pred_mask,
                                                            cls_id, pred_class_score_mod,
                                                            self.class_names,is_valid)
                                    this_pred.append(ann_save)
                                    objectness_scores.append(objectness)
                                    class_scores.append(pred_class_score)
                                    #raw_masks.append(raw_mask)
                                    if self.save_gates:
                                        for key in sample2gates[sample_idx].keys():
                                            for i_g in gates[key].keys():
                                                value = gates[key][i_g][i,s].squeeze()
                                                if i_g not in sample2gates[sample_idx][key].keys():
                                                    sample2gates[sample_idx][key][i_g] = []
                                                sample2gates[sample_idx][key][i_g].append(value)
                            predictions.append(ann)
                argmax_preds[sample_idx] = {}
                argmax_preds[sample_idx]['anns'] = this_pred
                argmax_preds[sample_idx]['width'] = im.shape[1]
                argmax_preds[sample_idx]['height'] = im.shape[0]
                argmax_preds[sample_idx]['objectness'] = objectness_scores
                argmax_preds[sample_idx]['class'] = class_scores
                #argmax_preds[sample_idx]['raw'] = raw_masks
                #print objectness_scores
                if self.display:
                    figures_dir = os.path.join('../models',args.model_name, args.model_name+'_figs_' + args.eval_split)
                    make_dir(figures_dir)

                    plt.figure();plt.axis('off')
                    plt.figure();plt.axis('off')
                    plt.imshow(im)
                    display_masks(this_pred, self.colors, im_height=im.shape[0],
                                im_width=im.shape[1],
                                no_display_text=self.args.no_display_text,
                                flip=self.flip_in_eval)

                    if self.dataset == 'coco':
                        sample_idx = self.coco.loadImgs(sample_idx)[0]['file_name'][:-4]
                    if self.dataset == 'cityscapes':
                        sample_idx = sample_idx.split('/')[-1]
                    if self.dataset == 'leaves':
                        sample_idx = sample_idx.split('/')[-1]

                    if self.flip_in_eval:
                        figname = os.path.join(figures_dir, sample_idx+'_flip')
                    else:
                        figname = os.path.join(figures_dir, sample_idx)
                    plt.savefig(figname,bbox_inches='tight')
                    plt.close()

                    # uncomment to display ground truth masks
                    gt_anns = self.key_to_anns[sample_idx]
                    im = imread(image_dir)
                    plt.figure();plt.axis('off')
                    plt.imshow(im)
                    display_masks(gt_anns, self.colors, im_height=im.shape[0],
                                im_width=im.shape[1],
                                no_display_text=self.args.no_display_text)

                    plt.savefig(os.path.join(figures_dir,sample_idx+'_gt.png'),bbox_inches='tight')
                    plt.close()
                    

            acc_samples+=np.shape(out_masks)[0]

        with open(os.path.join('../models',args.model_name,'argmax_preds_'+args.eval_split+'.pkl'),'wb') as f:
            pickle.dump(argmax_preds,f,protocol=pickle.HIGHEST_PROTOCOL)

        if self.save_gates:
            with open(os.path.join('../models',args.model_name,'gates_'+args.eval_split+'.pkl'),'wb') as f:
                pickle.dump(sample2gates,f,protocol=pickle.HIGHEST_PROTOCOL)
        return predictions

    def run_eval(self):
        print "Dataset is %s"%(self.dataset)
        print "Split is %s"%(self.split)
        print "Evaluating for %d images"%(len(self.sample_list))
        print "Number of classes is %d"%(len(self.class_names))

        if self.dataset == 'pascal':
            cocoGT = self.coco.loadRes(self.gt_file)
        elif self.dataset == 'coco':
            cocoGT = self.coco
        predictions = self._create_json()

        cocoP = self.coco.loadRes(predictions)
        cocoEval = COCOeval(cocoGT, cocoP, 'segm')

        cocoEval.params.maxDets = [1,args.max_dets,100]
        cocoEval.params.useCats = args.use_cats
        if not args.cat_id == -1:
            cocoEval.params.catIds = [args.cat_id]

        cocoEval.params.imgIds = sorted(self.sample_list)
        cocoEval.params.catIds = range(1, len(self.class_names))

        print ("Results for all the classes together")
        cocoEval.evaluate()
        cocoEval.accumulate()
        cocoEval.summarize()
        if self.all_classes:
            for actual_class in cocoEval.params.catIds:
                print ("Testing class dataset_id: " + str(actual_class))
                print ("Which corresponds to name: " + self.class_names[actual_class])
                cocoEval.params.catIds = [actual_class]
                cocoEval.evaluate()
                cocoEval.accumulate()
                cocoEval.summarize()


if __name__ == "__main__":

    parser = get_parser()
    args = parser.parse_args()
    torch.manual_seed(args.seed)

    if not args.log_term:
        print "Eval logs will be saved to:", os.path.join('../models',args.model_name, 'eval.log')
        sys.stdout = open(os.path.join('../models',args.model_name, 'eval.log'), 'w')

    if args.use_gpu:
        torch.cuda.manual_seed(args.seed)
    E = Evaluate(args)
    E.run_eval()
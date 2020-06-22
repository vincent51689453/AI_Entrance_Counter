import sys
import time
import logging
import argparse

import numpy as np
import cv2
import tensorflow as tf
import tensorflow.contrib.tensorrt as trt
import centroidtracker as ot
import imutils
import csv
import datetime

from utils.camera import add_camera_args, Camera
from utils.od_utils import read_label_map, build_trt_pb, load_trt_pb, \
                           write_graph_tensorboard, detect
#from utils.visualization import BBoxVisualization
import math

ALPHA = 0.5
FONT = cv2.FONT_HERSHEY_PLAIN
TEXT_SCALE = 1.0
TEXT_THICKNESS = 1
BLACK = (0,0,0)
WHITE = (255,255,255)

args = None


ct = ot.CentroidTracker()
temp=[]

# Constants
DEFAULT_MODEL = 'ssd_mobilenet_v1_coco'
DEFAULT_LABELMAP = 'third_party/models/research/object_detection/' \
                   'data/mscoco_label_map.pbtxt'
WINDOW_NAME = 'Guardian'
BBOX_COLOR = (0, 255, 0)  # green


def parse_args():
    """Parse input arguments."""
    desc = ('This script captures and displays live camera video, '
            'and does real-time object detection with TF-TRT model '
            'on Jetson TX2/TX1/Nano')
    parser = argparse.ArgumentParser(description=desc)
    parser = add_camera_args(parser)
    parser.add_argument('--model', dest='model',
                        help='tf-trt object detecion model '
                        '[{}]'.format(DEFAULT_MODEL),
                        default=DEFAULT_MODEL, type=str)
    parser.add_argument('--build', dest='do_build',
                        help='re-build TRT pb file (instead of using'
                        'the previously built version)',
                        action='store_true')
    parser.add_argument('--tensorboard', dest='do_tensorboard',
                        help='write optimized graph summary to TensorBoard',
                        action='store_true')
    parser.add_argument('--labelmap', dest='labelmap_file',
                        help='[{}]'.format(DEFAULT_LABELMAP),
                        default=DEFAULT_LABELMAP, type=str)
    parser.add_argument('--num-classes', dest='num_classes',
                        help='(deprecated and not used) number of object '
                        'classes', type=int)
    parser.add_argument('--confidence', dest='conf_th',
                        help='confidence threshold [0.3]',
                        default=0.3, type=float)
    args = parser.parse_args()
    return args


def open_display_window(width, height):
    """Open the cv2 window for displaying images with bounding boxeses."""
    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WINDOW_NAME, width, height)
    cv2.moveWindow(WINDOW_NAME, 0, 0)
    cv2.setWindowTitle(WINDOW_NAME, 'Guardian[IP CAM 001]')


def draw_help_and_fps(img, fps):
    """Draw help message and fps number at top-left corner of the image."""
    help_text = "'Esc' to Quit, 'H' for FPS & Help, 'F' for Fullscreen"
    optical_text = "'P' to toggle Optical Flow Tracing"
    font = cv2.FONT_HERSHEY_PLAIN
    line = cv2.LINE_AA

    fps_text = 'FPS: {:.1f}'.format(fps)
    cv2.putText(img, help_text, (11, 20), font, 1.0, (32, 32, 32), 4, line)
    cv2.putText(img, help_text, (10, 20), font, 1.0, (240, 240, 240), 1, line)
    cv2.putText(img, fps_text, (11, 80), font, 1.0, (32, 32, 32), 4, line)
    cv2.putText(img, fps_text, (10, 80), font, 1.0, (240, 240, 240), 1, line)
    cv2.putText(img, optical_text, (11, 50), font, 1.0, (32, 32, 32), 4, line)
    cv2.putText(img, optical_text, (10, 50), font, 1.0, (240, 240, 240), 1, line)
    return img


def set_full_screen(full_scrn):
    """Set display window to full screen or not."""
    prop = cv2.WINDOW_FULLSCREEN if full_scrn else cv2.WINDOW_NORMAL
    cv2.setWindowProperty(WINDOW_NAME, cv2.WND_PROP_FULLSCREEN, prop)


def gen_colors(num_colors):
    """Generate different colors.

    # Arguments
      num_colors: total number of colors/classes.

    # Output
      bgrs: a list of (B, G, R) tuples which correspond to each of
            the colors/classes.
    """
    import random
    import colorsys

    hsvs = [[float(x) / num_colors, 1., 0.7] for x in range(num_colors)]
    random.seed(1234)
    random.shuffle(hsvs)
    rgbs = list(map(lambda x: list(colorsys.hsv_to_rgb(*x)), hsvs))
    bgrs = [(int(rgb[2] * 255), int(rgb[1] * 255),  int(rgb[0] * 255))
            for rgb in rgbs]
    return bgrs


def draw_boxed_text(img, text, topleft, color):
    """Draw a transluent boxed text in white, overlayed on top of a
    colored patch surrounded by a black border. FONT, TEXT_SCALE,
    TEXT_THICKNESS and ALPHA values are constants (fixed) as defined
    on top.

    # Arguments
      img: the input image as a numpy array.
      text: the text to be drawn.
      topleft: XY coordinate of the topleft corner of the boxed text.
      color: color of the patch, i.e. background of the text.

    # Output
      img: note the original image is modified inplace.
    """
    assert img.dtype == np.uint8
    img_h, img_w, _ = img.shape
    if topleft[0] >= img_w or topleft[1] >= img_h:
        return
    margin = 3
    size = cv2.getTextSize(text, FONT, TEXT_SCALE, TEXT_THICKNESS)
    w = size[0][0] + margin * 2
    h = size[0][1] + margin * 2
    # the patch is used to draw boxed text
    patch = np.zeros((h, w, 3), dtype=np.uint8)
    patch[...] = color
    cv2.putText(patch, text, (margin+1, h-margin-2), FONT, TEXT_SCALE,
                WHITE, thickness=TEXT_THICKNESS, lineType=cv2.LINE_8)
    cv2.rectangle(patch, (0, 0), (w-1, h-1), BLACK, thickness=1)
    w = min(w, img_w - topleft[0])  # clip overlay at image boundary
    h = min(h, img_h - topleft[1])
    # Overlay the boxed text onto region of interest (roi) in img
    roi = img[topleft[1]:topleft[1]+h, topleft[0]:topleft[0]+w, :]
    cv2.addWeighted(patch[0:h, 0:w, :], ALPHA, roi, 1 - ALPHA, 0, roi)
    return img


class BBoxVisualization():
    """BBoxVisualization class implements nice drawing of boudning boxes.

    # Arguments
      cls_dict: a dictionary used to translate class id to its name.
    """

    def __init__(self, cls_dict):
        self.cls_dict = cls_dict
        self.colors = gen_colors(len(cls_dict))

    def draw_bboxes(self, img, box, conf, cls):
        """Draw detected bounding boxes on the original image.""" 
        global rects,ct,temp
        for bb, cf, cl in zip(box, conf, cls):
            cl = int(cl)
            if((cl == 1)and(cf>=0.7)): 
                y_min, x_min, y_max, x_max = bb[0], bb[1], bb[2], bb[3]
                color = self.colors[cl]
                temp.append(x_min)
                temp.append(y_min)
                temp.append(x_max)
                temp.append(y_max)
                rects.append(temp)
                temp=[]
                cv2.rectangle(img, (x_min, y_min), (x_max, y_max), color, 2)
                txt_loc = (max(x_min+2, 0), max(y_min+2, 0))
                cls_name = self.cls_dict.get(cl, 'CLS{}'.format(cl))
                txt = '{} {:.2f}'.format(cls_name, cf)
                img = draw_boxed_text(img, txt, txt_loc, color)
       
        return img


def loop_and_detect(cam, tf_sess, conf_th, vis, od_type):
    """Loop, grab images from camera, and do object detection.

    # Arguments
      cam: the camera object (video source).
      tf_sess: TensorFlow/TensorRT session to run SSD object detection.
      conf_th: confidence/score threshold for object detection.
      vis: for visualization.
    """
    show_fps = True
    full_scrn = False
    fps = 0.0
    tic = time.time()
    tracks = []
    global rects,ct,temp,args
    frame_buff = 0
    none_buff = 0
    restart_flag = False
    backup_label = None
    while True:
        #if cv2.getWindowProperty(WINDOW_NAME, 0) < 0:
        # Check to see if the user has closed the display window.
        # If yes, terminate the while loop.
        #    break
        if(restart_flag == True):
            cam = Camera(args)
            cam.open()
            cam.start()
            #pb_path = './data/{}_trt.pb'.format(args.model)
            #log_path = './logs/{}_trt'.format(args.model)
            #trt_graph = load_trt_pb(pb_path)
            #tf_config = tf.ConfigProto()
            #tf_config.gpu_options.allow_growth = True
            #tf_sess = tf.Session(config=tf.ConfigProto(allow_soft_placement=True,log_device_placement=True),graph=trt_graph)
            #od_type = 'faster_rcnn' if 'faster_rcnn' in args.model else 'ssd'
            dummy_img = np.zeros((720, 1280, 3), dtype=np.uint8)
            _, _, _ = detect(dummy_img, tf_sess, conf_th=.3, od_type=od_type)
            restart_flag = False


        rects = []
        img = cam.read()
        optical_flow_image = img
        if img is not None:
            box, conf, cls = detect(img, tf_sess, conf_th, od_type=od_type)    
            img = vis.draw_bboxes(img, box, conf, cls)
            objects = ct.update(rects)
            cv2.rectangle(img, (0,980),(1920,1075),(0,0,0),-1)
           
            for (objectID, centroid) in objects.items():
	        # draw both the ID of the object and the centroid of the
	        # object on the output frame
                text = "ID {}".format(objectID)
                cv2.putText(img, text, (centroid[0] - 10, centroid[1] - 10),cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255,0,255), 2)
                cv2.circle(img, (centroid[0], centroid[1]), 4, (255,0,255), -1) 
                backup_label = str(objectID)  
                   
                
            cv2.putText(img, backup_label, (330,1035),cv2.FONT_HERSHEY_TRIPLEX,1,(255,255,255),2,cv2.LINE_AA)
            sys_clock = str(datetime.datetime.now())+" Frame_buff="+str(frame_buff)
            print(sys_clock)
            cv2.putText(img, sys_clock, (20,950), cv2.FONT_HERSHEY_TRIPLEX,1,(0,0,255),2,cv2.LINE_AA)
            cv2.putText(img, "Traffic Counter: ", (20,1035), cv2.FONT_HERSHEY_TRIPLEX,1,(0,0,255),2,cv2.LINE_AA)
            cv2.putText(img, "Detector Type: Human", (400,1035), cv2.FONT_HERSHEY_TRIPLEX,1,(0,0,255),2,cv2.LINE_AA)
            cv2.putText(img, "Real Time Optical Trace :", (900,1035), cv2.FONT_HERSHEY_TRIPLEX,1,(0,0,255),2,cv2.LINE_AA)
            cv2.putText(img, "OFF", (1380,1035), cv2.FONT_HERSHEY_TRIPLEX,1,(255,255,255),2,cv2.LINE_AA)   
            """   
            if(frame_buff == 2000):
                print("[SYSTEM] VSTARCAMERA Restart")
                cam.stop()  # terminate the sub-thread in camera
                #tf_sess.close()
                #tf.reset_default_graph()
                #tf.contrib.keras.backend.clear_session()
                cam.release() 
                restart_flag = True
                frame_buff = 0
                img = None
                cv2.destroyAllWindows()
            frame_buff += 1 
            """
            if(restart_flag == False):
                if show_fps:
                    img = draw_help_and_fps(img, fps)
                #set_full_screen(full_scrn)
                cv2.moveWindow(WINDOW_NAME,0,0)
                cv2.imshow(WINDOW_NAME, img)
                toc = time.time()
                curr_fps = 1.0 / (toc - tic)
                # calculate an exponentially decaying average of fps number
                fps = curr_fps if fps == 0.0 else (fps*0.9 + curr_fps*0.1)
                tic = toc
        else:
            print("None Image  --> None Buff = {}".format(none_buff))
            none_buff+=1
            if(none_buff == 500):
                print("[SYSTEM] VSTARCAMERA Restart")
                cam.stop()  # terminate the sub-thread in camera
                #tf_sess.close()
                #tf.reset_default_graph()
                #tf.contrib.keras.backend.clear_session()
                cam.release() 
                restart_flag = True
                none_buff = 0
                img = None
                cv2.destroyAllWindows()
 


  
        if(restart_flag == False):
            key = cv2.waitKey(1)
            if key == 27:  # ESC key: quit program
                break
            elif key == ord('H') or key == ord('h'):  # Toggle help/fps
                show_fps = not show_fps
            elif key == ord('F') or key == ord('f'):  # Toggle fullscreen
                full_scrn = not full_scrn
                set_full_screen(full_scrn)

    

def main():
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    # Ask tensorflow logger not to propagate logs to parent (which causes
    # duplicated logging)
    logging.getLogger('tensorflow').propagate = False
    global args

    args = parse_args()
    logger.info('called with args: %s' % args)

    # build the class (index/name) dictionary from labelmap file
    logger.info('reading label map')
    cls_dict = read_label_map(args.labelmap_file)

    pb_path = './data/{}_trt.pb'.format(args.model)
    log_path = './logs/{}_trt'.format(args.model)
    if args.do_build:
        logger.info('building TRT graph and saving to pb: %s' % pb_path)
        build_trt_pb(args.model, pb_path)

    logger.info('opening camera device/file')
    cam = Camera(args)
    cam.open()
    if not cam.is_opened:
        sys.exit('Failed to open camera!')

    logger.info('loading TRT graph from pb: %s' % pb_path)
    trt_graph = load_trt_pb(pb_path)

    logger.info('starting up TensorFlow session')
    tf_config = tf.ConfigProto()
    tf_config.gpu_options.allow_growth = True
    #tf_sess = tf.Session(config=tf_config, graph=trt_graph) -- Vincent
    #Solve : "unable to satfisfy explicit device /dev/CPU:0 -- Vincent
    tf_sess = tf.Session(config=tf.ConfigProto(allow_soft_placement=True,log_device_placement=True),graph=trt_graph)
    if args.do_tensorboard:
        logger.info('writing graph summary to TensorBoard')
        write_graph_tensorboard(tf_sess, log_path)

    logger.info('warming up the TRT graph with a dummy image')
    od_type = 'faster_rcnn' if 'faster_rcnn' in args.model else 'ssd'
    dummy_img = np.zeros((720, 1280, 3), dtype=np.uint8)
    _, _, _ = detect(dummy_img, tf_sess, conf_th=.3, od_type=od_type)

    cam.start()  # ask the camera to start grabbing images
    # grab image and do object detection (until stopped by user)
    logger.info('starting to loop and detect')
    vis = BBoxVisualization(cls_dict)
    open_display_window(cam.img_height, cam.img_width)
    result=loop_and_detect(cam, tf_sess, args.conf_th, vis, od_type=od_type)
    logger.info('cleaning up')
    cam.stop()  # terminate the sub-thread in camera
    tf_sess.close()
    cam.release()
    cv2.destroyAllWindows()


if __name__ == '__main__':
    main()

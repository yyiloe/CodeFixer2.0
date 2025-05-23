import os
import torch
import time
import numpy as np
import pickle
import random
from model import MultiClassifier
from gensim.models.word2vec import Word2Vec


def get_batch(x, y, idx, bs):
    x_batch = x[idx: idx + bs]
    y_batch = y[idx: idx + bs]
    return torch.LongTensor(x_batch), torch.LongTensor(y_batch)


def load_from_file(file_path):
    with open(file_path, "rb") as file:
        return pickle.load(file)


def print_parameter_statistics(model):
    total_num = [p.numel() for p in model.parameters()]
    trainable_num = [p.numel() for p in model.parameters() if p.requires_grad]
    print("Total parameters: {}".format(sum(total_num)))
    print("Trainable parameters: {}".format(sum(trainable_num)))


def co_teaching_loss(model1_loss, model2_loss, rt):
    _, model1_sm_idx = torch.topk(model1_loss, k=int(int(model1_loss.size(0)) * rt), largest=False)
    _, model2_sm_idx = torch.topk(model2_loss, k=int(int(model2_loss.size(0)) * rt), largest=False)

    # co-teaching
    model1_loss_filter = torch.zeros((model1_loss.size(0))).cuda()
    model1_loss_filter[model2_sm_idx] = 1.0
    model1_loss = (model1_loss_filter * model1_loss).mean()

    model2_loss_filter = torch.zeros((model2_loss.size(0))).cuda()
    model2_loss_filter[model1_sm_idx] = 1.0
    model2_loss = (model2_loss_filter * model2_loss).mean()

    return model1_loss, model2_loss


if __name__ == "__main__":
    root = "data/"

    train_x = load_from_file(root + 'train/x_w2v_.pkl')
    val_x = load_from_file(root + 'val/x_w2v_.pkl')
    test_x = load_from_file(root + 'test/x_w2v_.pkl')

    train_y = load_from_file(root + 'train/y_.pkl')
    val_y = load_from_file(root + 'val/y_.pkl')
    test_y = load_from_file(root + 'test/y_.pkl')

    word2vec = Word2Vec.load(root + "train/embedding/w2v_512").wv
    #embeddings = np.zeros((word2vec.syn0.shape[0] + 2, word2vec.syn0.shape[1]), dtype="float32")
    embeddings = np.zeros((word2vec.vectors.shape[0] + 2, word2vec.vectors.shape[1]), dtype="float32")
    embeddings[:word2vec.vectors.shape[0]] = word2vec.vectors

    TOKEN_LENGTH = 400
    HIDDEN_DIM = 80
    EPOCHS = 400
    BATCH_SIZE = 64 #64
    LABELS = 11
    USE_GPU = True
   # MAX_TOKENS = word2vec.syn0.shape[0] + 2
    MAX_TOKENS = word2vec.vectors.shape[0] + 2
    EMBEDDING_DIM = word2vec.vectors.shape[1]

    t = 0.0002

    print("MAX_TOKENS:{}".format(MAX_TOKENS))
    print("EMBEDDING_DIM:{}".format(EMBEDDING_DIM))
    print("HIDDEN_DIM: {}".format(HIDDEN_DIM))
    print("BATCH_SIZE: {}".format(BATCH_SIZE))

    model = MultiClassifier(embeddings, EMBEDDING_DIM, MAX_TOKENS, 400, 0.1, 1, 11, 8)
    model_d = MultiClassifier(embeddings, EMBEDDING_DIM, MAX_TOKENS, 400, 0.1, 1, 11, 8)
    ##1024 0.1 0.001 1e-3 8
    ##512 0.3 0.001 1e-4 4
    if USE_GPU:
        model.cuda()
        model_d.cuda()
    #model.load_state_dict(torch.load("./model_save/model_params.pkl"))
    parameters = model.parameters()
    optimizer = torch.optim.Adam(parameters, lr=0.001,weight_decay=1e-3)
    parameters_d = model_d.parameters()
    optimizer_d = torch.optim.Adam(parameters_d, lr=0.001,weight_decay=1e-3)
    print("Optimizer: {}".format(type(optimizer).__name__))
    loss_function = torch.nn.CrossEntropyLoss()
    loss_function_none = torch.nn.CrossEntropyLoss(reduction='none')
    print("Loss function: {}".format(type(loss_function).__name__))
    print_parameter_statistics(model)

    train_loss_ = []
    val_loss_ = []

    train_acc_ = []
    val_acc_ = []

    train_mrr_ = []
    val_mrr_ = []

    best_val_mrr = 0.0
    print('Start training ...')
    for epoch in range(EPOCHS):
        start_time = time.time()

        # shuffle data before training for each epoch
        random.seed(666)
        random.shuffle(train_x)
        random.seed(666)
        random.shuffle(train_y)

        # training phase
        model.train()
        total_acc = 0.0
        total_mrr = 0.0
        total_loss = 0.0
        total = 0

        i = 0
        while i < len(train_x):
            batch = get_batch(train_x, train_y, i, BATCH_SIZE)
            train_inputs, train_labels = batch
            if USE_GPU:
                train_inputs = train_inputs.cuda()
                train_labels = train_labels.cuda()
            i += BATCH_SIZE
            model.zero_grad()
            model_d.zero_grad()
            output = model(train_inputs)
            output_d = model_d(train_inputs)

            R = 1 - t * epoch
            loss = loss_function_none(output, train_labels)
            loss_d = loss_function_none(output_d, train_labels)
            filter_loss, filter_loss_d = co_teaching_loss(loss, loss_d, R)

            filter_loss.backward()
            optimizer.step()

            filter_loss_d.backward()
            optimizer_d.step()

            _, predicted = torch.max(output.data, 1)
            total_acc += (predicted == train_labels).sum().item()
            total_mrr += (1 / (torch.where(
                (output.topk(output.shape[1], 1, True, True).indices.t() - train_labels.t()).t() == 0)[
                                   1].float() + 1)).sum().item()
            total += len(train_inputs)
            total_loss += loss.mean().item() * len(train_inputs)

        train_loss_.append(total_loss / total)
        train_acc_.append(total_acc / total)
        train_mrr_.append(total_mrr / total)

        # validating phase
        model.eval()
        total_acc = 0.0
        total_mrr = 0.0
        total_loss = 0.0
        total = 0

        i = 0
        while i < len(val_x):
            batch = get_batch(val_x, val_y, i, BATCH_SIZE)
            val_inputs, val_labels = batch
            if USE_GPU:
                val_inputs = val_inputs.cuda()
                val_labels = val_labels.cuda()
            i += BATCH_SIZE
            output = model(val_inputs)
            loss = loss_function(output, val_labels)

            _, predicted = torch.max(output.data, 1)
            total_acc += (predicted == val_labels).sum().item()
            total_mrr += (1 / (torch.where(
                (output.topk(output.shape[1], 1, True, True).indices.t() - val_labels.t()).t() == 0)[
                                   1].float() + 1)).sum().item()
            total += len(val_inputs)
            total_loss += loss.item() * len(val_inputs)
        val_loss_.append(total_loss / total)
        val_acc_.append(total_acc / total)
        val_mrr_.append(total_mrr / total)

        # save model
        if val_mrr_[-1] > best_val_mrr:
            model_save_dir = "./model_save/"
            if not os.path.exists(model_save_dir):
                os.makedirs(model_save_dir)
            torch.save(model.state_dict(), os.path.join(model_save_dir, "model_params.pkl"))
            torch.save(model, os.path.join(model_save_dir, "model.pkl"))
            best_val_mrr = val_mrr_[-1]
            print("=" * 30 + "\n" + "Saving model: epoch_{}".format(epoch + 1))

        end_time = time.time()
        print('[Epoch: %3d/%3d] Training Loss: %.4f, Validation Loss: %.4f,'
              ' Training Acc: %.3f, Validation Acc: %.3f,'
              ' Training Mrr: %.3f, Validation Mrr: %.3f,'
              ' Time Cost: %.3f s'
              % (epoch + 1, EPOCHS, train_loss_[epoch], val_loss_[epoch],
                 train_acc_[epoch], val_acc_[epoch],
                 train_mrr_[epoch], val_mrr_[epoch],
                 end_time - start_time))
        if epoch !=0 and  epoch % 150 == 0:
            for p in optimizer.param_groups:
                p['lr'] *= 0.5
            for p in optimizer_d.param_groups:
                p['lr'] *= 0.5


    # testing phase
    total_acc = 0.0
    total_mrr = 0.0
    total_loss = 0.0
    total = 0

    model.load_state_dict(torch.load("./model_save/model_params.pkl"))
    model.eval()

    i = 0
    while i < len(test_x):
        batch = get_batch(test_x, test_y, i, BATCH_SIZE)
        test_inputs, test_labels = batch
        if USE_GPU:
            test_inputs = test_inputs.cuda()
            test_labels = test_labels.cuda()
        i += BATCH_SIZE
        output = model(test_inputs)

        loss = loss_function(output, test_labels)

        _, predicted = torch.max(output.data, 1)
        total_acc += (predicted == test_labels).sum().item()
        total_mrr += (1 / (
                torch.where((output.topk(output.shape[1], 1, True, True).indices.t() - test_labels.t()).t() == 0)[
                    1].float() + 1)).sum().item()
        total += len(test_inputs)
        total_loss += loss.item() * len(test_inputs)
    print("Testing results(Acc):", total_acc / total)
    print("Testing results(Mrr):", total_mrr / total)

import torch
import torch.nn as nn
import numpy as np
from copy import deepcopy

import disjoint_domain as dd


class DisjointDomainNet(nn.Module):
    """
    Network for disjoint domain learning as depicted in Figure R4.
    
    item_repr_compression: 
    Contains separate item representation and context representation layers,
    unless "merged" is True, in which case there is a common representation layer.
    """

    def gen_training_tensors(self):
        """Make PyTorch x and y tensors for training DisjointDomainNet"""

        item_mat, context_mat, attr_mat = dd.make_io_mats(
            ctx_per_domain=self.ctx_per_domain, attrs_per_context=self.attrs_per_context,
            n_domains=self.n_domains, simplified=self.simple_item_tree)

        x_item = torch.tensor(item_mat, dtype=self.torchfp, device=self.device)
        x_context = torch.tensor(context_mat, dtype=self.torchfp, device=self.device)
        y = torch.tensor(attr_mat, dtype=self.torchfp, device=self.device)

        return x_item, x_context, y

    def __init__(self, ctx_per_domain, attrs_per_context, n_domains, item_repr_units=16,
                 ctx_repr_units=16, hidden_units=32, rng_seed=None, torchfp=None,
                 device=None, merged_repr=False, use_item_repr=True, use_ctx_repr=True,
                 simple_item_tree=False):
        super(DisjointDomainNet, self).__init__()
        
        assert (not merged_repr) or (use_item_repr and use_ctx_repr), "Can't both skip and merge repr layers"

        self.ctx_per_domain = ctx_per_domain
        self.attrs_per_context = attrs_per_context
        self.n_domains = n_domains
        self.n_items = dd.ITEMS_PER_DOMAIN * n_domains
        self.n_contexts = ctx_per_domain * n_domains
        self.n_attributes = attrs_per_context * self.n_contexts
        self.merged_repr = merged_repr
        self.use_item_repr = use_item_repr
        self.use_ctx_repr = use_ctx_repr
        self.simple_item_tree = simple_item_tree
        
        self.dummy_item = torch.zeros((1, self.n_items))
        self.dummy_ctx = torch.zeros((1, self.n_contexts))

        if rng_seed is None:
            torch.seed()
        else:
            torch.manual_seed(rng_seed)

        self.device, self.torchfp = dd.init_torch(device, torchfp)

        self.item_repr_size = item_repr_units
        self.ctx_repr_size = ctx_repr_units
        self.hidden_size = hidden_units
        
        self.repr_size = item_repr_units + ctx_repr_units
        if self.merged_repr:
            # inputs should map to full repr layer
            self.item_repr_size = self.repr_size
            self.ctx_repr_size = self.repr_size
        else:
            if not self.use_item_repr:
                self.repr_size += self.n_items - item_repr_units

            if not self.use_ctx_repr:
                self.repr_size += self.n_contexts - ctx_repr_units
        
        # define layers
        self.item_to_rep = (nn.Linear(self.n_items, self.item_repr_size).to(device)
                            if self.use_item_repr else nn.Identity())
        self.ctx_to_rep = (nn.Linear(self.n_contexts, self.ctx_repr_size).to(device)
                           if self.use_ctx_repr else nn.Identity())
        
        self.rep_to_hidden = nn.Linear(self.repr_size, self.hidden_size).to(device)        
        self.hidden_to_attr = nn.Linear(self.hidden_size, self.n_attributes).to(device)

        # make weights start small
        with torch.no_grad():
            for p in self.parameters():
                nn.init.normal_(p.data, std=0.01)
                # nn.init.uniform_(p.data, a=-0.01, b=0.01)

        # make some data
        self.x_item, self.x_context, self.y = self.gen_training_tensors()
        self.n_inputs = len(self.y)

        # individual item/context tensors for evaluating the network
        self.items, self.item_names = dd.get_items(n_domains=n_domains)
        self.contexts, self.context_names = dd.get_contexts(n_domains=n_domains, ctx_per_domain=ctx_per_domain)

        self.criterion = nn.BCELoss(reduction='sum')

    def calc_item_repr(self, item):
        assert self.use_item_repr, 'No item representation to calculate'
        return torch.sigmoid(self.item_to_rep(item))
#         if not self.use_item_repr:
#             # have to go to hidden layer to get something other than the input
#             crep = torch.sigmoid(self.ctx_to_rep(self.dummy_ctx))
#             repr_cat = torch.cat((irep, crep.repeat(irep.shape[0], 1)), dim=1)
#             return torch.sigmoid(self.rep_to_hidden(repr_cat))
        
#         return irep
    
    def calc_context_repr(self, context):
        assert self.use_ctx_repr, 'No context representation to calculate'
        return torch.sigmoid(self.ctx_to_rep(context))
#         if not self.use_ctx_repr:
#             # have to go to hidden layer
#             irep = torch.sigmoid(self.item_to_rep(self.dummy_item))
#             repr_cat = torch.cat((irep.repeat(crep.shape[0], 1), crep), dim=1)
#             return torch.sigmoid(self.rep_to_hidden(repr_cat))
        
#         return crep

    def calc_hidden(self, item=None, context=None):
        if item is None:
            item = self.dummy_item.repeat((context.shape[0] if context is not None else 1), 1)
        if context is None:
            context = self.dummy_ctx.repeat(item.shape[0], 1)
            
        irep = self.item_to_rep(item)
        crep = self.ctx_to_rep(context)

        if self.merged_repr:
            rep = irep + crep
        else:
            rep = torch.cat((irep, crep), dim=1)
        rep = torch.sigmoid(rep)
        return torch.sigmoid(self.rep_to_hidden(rep))

    def forward(self, item, context):
        hidden = self.calc_hidden(item, context)
        attr = torch.sigmoid(self.hidden_to_attr(hidden))        
        return attr

    def b_outputs_correct(self, outputs, batch_inds):
        """Element-wise function to find which outputs are correct for a batch"""
        return torch.lt(torch.abs(outputs - self.y[batch_inds]), 0.1).to(self.torchfp)
    
    def weighted_acc(self, outputs, batch_inds):
        """
        For each item in the batch, find the average of accuracy for 0s and accuracy for 1s
        (i.e. correct for unbalanced ground truth output)
        """
        total_attrs = self.attrs_per_context * self.n_contexts
        set_attrs = 25
        unset_attrs = total_attrs - set_attrs
        
        set_weight = 0.5 / set_attrs
        unset_weight = 0.5 / unset_attrs
        
        weights = torch.where(self.y[batch_inds].to(bool), set_weight, unset_weight)
        b_correct = self.b_outputs_correct(outputs, batch_inds)
        return torch.sum(weights * b_correct, dim=1)

    def train_epoch(self, order, batch_size, optimizer):
        """
        Do training on batches of given size of the examples indexed by order.
        Return the total loss and output accuracy for each example (in index order).
        Accuracy for any example that is not used will be nan.
        """
        total_loss = torch.tensor(0.0)
        acc_each = torch.full((self.n_inputs,), np.nan)
        wacc_each = torch.full((self.n_inputs,), np.nan)
        if type(order) != torch.Tensor:
            order = torch.tensor(order, device='cpu', dtype=torch.long)
        
        for batch_inds in torch.split(order, batch_size):
            optimizer.zero_grad()
            outputs = self(self.x_item[batch_inds], self.x_context[batch_inds])
            loss = self.criterion(outputs, self.y[batch_inds])
            loss.backward()
            optimizer.step()

            with torch.no_grad():
                total_loss += loss
                acc_each[batch_inds] = torch.mean(self.b_outputs_correct(outputs, batch_inds), dim=1)
                wacc_each[batch_inds] = self.weighted_acc(outputs, batch_inds)

        return total_loss, acc_each, wacc_each

    def prepare_holdout(self, holdout_item=True, holdout_context=True):
        """
        Pick an item and context to hold out during regular training. Then, at each epoch,
        the number of additional epochs needed to reach a threshold of accuracy on the held-out
        items and contexts is recorded.

        Returns vectors of indices into items, contexts, and x/y that will still be used. 
        """
        ho_item_domain, ho_ctx_domain = dd.choose_k_inds(self.n_domains, 2)
        ho_item_ind = ho_item_domain * dd.ITEMS_PER_DOMAIN + dd.choose_k_inds(dd.ITEMS_PER_DOMAIN, 1)
        if holdout_item:
            print(f'Holding out item: {self.item_names[ho_item_ind]}')
        ho_ctx_ind = ho_ctx_domain * self.ctx_per_domain + dd.choose_k_inds(self.ctx_per_domain, 1)
        if holdout_context:
            print(f'Holding out context: {self.context_names[ho_ctx_ind]}')

        train_item_inds = np.setdiff1d(range(self.n_items), ho_item_ind) if holdout_item else np.arange(self.n_items)
        train_ctx_inds = np.setdiff1d(range(self.n_contexts), ho_ctx_ind) if holdout_context else np.arange(self.n_contexts)

        # figure out which inputs are held out (item/context combinations)
        ho_item = self.items[ho_item_ind]
        ho_context = self.contexts[ho_ctx_ind]
        b_x_item_ho = self.x_item.eq(ho_item).all(axis=1).cpu()
        b_x_ctx_ho = self.x_context.eq(ho_context).all(axis=1).cpu()
        test_x_item_inds = torch.flatten(torch.nonzero(b_x_item_ho)) if holdout_item else []
        test_x_ctx_inds = torch.flatten(torch.nonzero(b_x_ctx_ho)) if holdout_context else []
        
        # prepare array of which items to use during training
        train_x_inds = np.setdiff1d(np.arange(self.n_inputs), np.concatenate([test_x_item_inds, test_x_ctx_inds]))

        return train_item_inds, train_ctx_inds, train_x_inds, test_x_item_inds, test_x_ctx_inds
    
    def prepare_combo_testing(self):
        """
        For each domain, pick one item/context pair to hold out.
        Hold out a different one for each domain (assume this is possible, for now).
        """
        item_domain_starts = torch.arange(self.n_domains, device='cpu') * dd.ITEMS_PER_DOMAIN
        ctx_domain_starts = torch.arange(self.n_domains, device='cpu') * self.ctx_per_domain
        ho_items = item_domain_starts + dd.choose_k_inds(dd.ITEMS_PER_DOMAIN, self.n_domains)
        ho_contexts = ctx_domain_starts + dd.choose_k_inds(self.ctx_per_domain, self.n_domains)
        print('Holding out: ' + ', '.join(
            [f'{self.item_names[ii]}/{self.context_names[ci]}' for ii, ci in zip(ho_items, ho_contexts)]
        ))
        
        train_x_inds = np.arange(self.n_inputs)
        test_x_inds = np.zeros(self.n_domains)
        
        # Find indices of held out combos in full input arrays
        for k_domain in range(self.n_domains):
            b_x_item_ho = self.x_item.eq(self.items[ho_items[k_domain]]).all(axis=1).cpu()
            b_x_ctx_ho = self.x_context.eq(self.contexts[ho_contexts[k_domain]]).all(axis=1).cpu()
            b_x_ho = b_x_item_ho & b_x_ctx_ho
            
            assert torch.sum(b_x_ho) == 1, 'Uh-oh'            
            ind = torch.flatten(torch.nonzero(b_x_ho))[0]
            train_x_inds = np.setdiff1d(train_x_inds, ind)
            test_x_inds[k_domain] = ind

        return train_x_inds, test_x_inds
            
    def prepare_snapshots(self, snap_freq, snap_freq_scale, num_epochs):
        """Make tensors to hold representation snapshots and return some relevant info"""

        # Find exactly which epochs to take snapshots (could be on log scale)
        snap_epochs = dd.calc_snap_epochs(snap_freq, snap_freq_scale, num_epochs)

        epoch_digits = len(str(snap_epochs[-1]))
        n_snaps = len(snap_epochs)
        
        snaps = {}
        if self.use_item_repr:
            snaps['item'] = torch.full((n_snaps, self.n_items, self.item_repr_size), np.nan)
        if self.use_ctx_repr:
            snaps['context'] = torch.full((n_snaps, self.n_contexts, self.ctx_repr_size), np.nan)

        snaps['item_hidden'] = torch.full((n_snaps, self.n_items, self.hidden_size), np.nan)
        snaps['context_hidden'] = torch.full((n_snaps, self.n_contexts, self.hidden_size), np.nan)
        
        return snap_epochs, epoch_digits, snaps


    def generalize_test(self, batch_size, optimizer, included_inds, targets, max_epochs=2000, thresh=0.99):
        """
        See how long it takes the network to reach accuracy threshold on target inputs,
        when training on items specified by included_inds. Then restore the parameters.
        
        'targets' can be an array of indices or a logical mask into the full set of inputs.
        """

        # Save original state of network to restore later
        net_state_dict = deepcopy(self.state_dict())
        optim_state_dict = deepcopy(optimizer.state_dict())

        epochs = 0
        while epochs < max_epochs:
            order = dd.choose_k(included_inds, len(included_inds))
            _, _, wacc_each = self.train_epoch(order, batch_size, optimizer)

            acc_targets = torch.mean(wacc_each[targets])
            if acc_targets >= thresh:
                break

            epochs += 1

        # Restore old state of network
        self.load_state_dict(net_state_dict)
        optimizer.load_state_dict(optim_state_dict)

        etg_string = str(epochs + 1) if epochs < max_epochs else '>' + str(max_epochs)
        return epochs, etg_string


    def do_training(self, lr, num_epochs, batch_size, report_freq,
                    snap_freq, snap_freq_scale='lin', scheduler=None,
                    holdout_testing='full', reports_per_test=1,
                    test_thresh=0.99, test_max_epochs=2000,
                    do_combo_testing=False, param_snapshots=False):
        """
        Train the network for the specified number of epochs, etc.
        Return representation snapshots, training reports, and snapshot/report epochs.
        
        Holdout testing: train with one entire item, context, or both excluded, then
        periodically (every `reports_per_test` reports) test how many epochs are needed
        to train network up to obtaining test_thresh accuracy on the held out inputs.
        
        Combo testing: For each domain, hold out one item/context pair. At each report time,
        test the accuracy of the network on the held-out items and contexts.
        
        If param snapshots is true, also returns all weights and biases of the network at
        each snapshot epoch.
        """
        
        optimizer = torch.optim.SGD(self.parameters(), lr=lr)
        
        if holdout_testing is not None:
            holdout_testing = holdout_testing.lower()
        do_holdout_testing = holdout_testing is not None and holdout_testing != 'none'
        holdout_item = holdout_testing in ['full', 'item']
        holdout_ctx = holdout_testing in ['full', 'context', 'ctx']
        
        if do_holdout_testing and do_combo_testing:
            raise NotImplementedError("That's too much, man - I'm not doing both holdout and combo testing!")
        
        train_item_inds = np.arange(self.n_items)
        train_ctx_inds = np.arange(self.n_contexts)
        train_x_inds = np.arange(self.n_inputs)
        test_x_item_inds = test_x_ctx_inds = None # for holdout testing
        included_inds_item = included_inds_ctx = None # for holdout testing
        test_x_inds = None # for combo testing
        
        if do_holdout_testing:
            (train_item_inds, train_ctx_inds, train_x_inds,
             test_x_item_inds, test_x_ctx_inds) = self.prepare_holdout(holdout_item, holdout_ctx)
            # which indices to use during testing
            included_inds_item = np.concatenate([train_x_inds, test_x_item_inds])
            included_inds_ctx = np.concatenate([train_x_inds, test_x_ctx_inds])
        elif do_combo_testing:
            train_x_inds, test_x_inds = self.prepare_combo_testing()
            
        # (for snapshots)
        train_items = self.items[train_item_inds]
        train_contexts = self.contexts[train_ctx_inds]

        etg_digits = len(str(test_max_epochs)) + 1
            
        n_inputs_train = len(train_x_inds)

        snap_epochs, epoch_digits, snaps = self.prepare_snapshots(snap_freq, snap_freq_scale, num_epochs)
        n_snaps = len(snap_epochs)

        params = {}
        if param_snapshots:
            params = {pname: torch.empty((n_snaps, *p.shape)) for pname, p in self.named_parameters()}

        n_report = (num_epochs-1) // report_freq + 1
        n_etg = (n_report-1) // reports_per_test + 1
        reports = dict()
        reports['loss'] = np.zeros(n_report)
        reports['accuracy'] = np.zeros(n_report)
        reports['weighted_acc'] = np.zeros(n_report)
        
        if holdout_item:
            reports['etg_item'] = np.zeros(n_etg, dtype=int) # "epochs to generalize"
            
        if holdout_ctx:
            reports['etg_context'] = np.zeros(n_etg, dtype=int)
        
        if do_combo_testing:
            reports['test_accuracy'] = np.zeros(n_report)
            reports['test_weighted_acc'] = np.zeros(n_report)

        for epoch in range(num_epochs):

            # collect snapshot
            if epoch in snap_epochs:
                k_snap = snap_epochs.index(epoch)

                with torch.no_grad():
                    
                    if 'item' in snaps:
                        snaps['item'][k_snap][train_item_inds] = self.calc_item_repr(train_items)
                        
                    if 'context' in snaps:
                        snaps['context'][k_snap][train_ctx_inds] = self.calc_context_repr(train_contexts)
                    
                    snaps['item_hidden'][k_snap][train_item_inds] = self.calc_hidden(item=train_items)
                    snaps['context_hidden'][k_snap][train_ctx_inds] = self.calc_hidden(context=train_contexts)
                    
                    if param_snapshots:
                        for pname, p in self.named_parameters():
                            params[pname][k_snap] = p

            # do training
            order = dd.choose_k(train_x_inds, n_inputs_train)
            loss, acc_each, wacc_each = self.train_epoch(order, batch_size, optimizer)
            if scheduler is not None:
                scheduler.step()

            # report progress
            if epoch % report_freq == 0:
                k_report = epoch // report_freq
                
                with torch.no_grad():
                    mean_loss = loss.item() / n_inputs_train
                    mean_acc = torch.nansum(acc_each).item() / n_inputs_train
                    mean_wacc = torch.nansum(wacc_each).item() / n_inputs_train

                report_str = f'Epoch {epoch:{epoch_digits}d} end: loss = {mean_loss:7.3f}, weighted acc = {mean_wacc:.3f}'

                reports['loss'][k_report] = mean_loss
                reports['accuracy'][k_report] = mean_acc
                reports['weighted_acc'][k_report] = mean_wacc

                if do_holdout_testing and k_report % reports_per_test == 0:
                    k_test = k_report // reports_per_test
                    
                    # Do item and context generalize tests separately
                    if holdout_item:
                        item_etg, item_etg_string = self.generalize_test(
                            batch_size, optimizer, included_inds_item, test_x_item_inds,
                            thresh=test_thresh, max_epochs=test_max_epochs
                        )
                        report_str += f', epochs for new item = {item_etg_string:>{etg_digits}}'
                        reports['etg_item'][k_test] = item_etg
                    
                    if holdout_ctx:
                        ctx_etg, ctx_etg_string = self.generalize_test(
                            batch_size, optimizer, included_inds_ctx, test_x_ctx_inds,
                            thresh=test_thresh, max_epochs=test_max_epochs
                        )
                        report_str += f', epochs for new context = {ctx_etg_string:>{etg_digits}}'
                        reports['etg_context'][k_test] = ctx_etg
                        
                if do_combo_testing:
                    with torch.no_grad():
                        outputs = self(self.x_item[test_x_inds], self.x_context[test_x_inds])
                        test_acc = torch.mean(self.b_outputs_correct(outputs, test_x_inds)).item()
                        test_wacc = torch.mean(self.weighted_acc(outputs, test_x_inds)).item()
                        
                        report_str += f', test weighted acc = {test_wacc:.3f}'
                        reports['test_accuracy'][k_report] = test_acc
                        reports['test_weighted_acc'][k_report] = test_wacc
                                        
                print(report_str)

        snaps_cpu = {stype: s.cpu().numpy() for stype, s in snaps.items()}
        ret_dict = {'snaps': snaps_cpu, 'reports': reports}
        
        if param_snapshots:
            ret_dict['params'] = {pname: p.cpu().numpy() for pname, p in params.items()}
        
        return ret_dict
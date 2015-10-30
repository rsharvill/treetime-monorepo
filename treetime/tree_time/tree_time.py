"""
Class, which contains methods to optimize branch lengths given the time
constraints set to leaves
"""
from __future__ import print_function, division

from tree_anc import TreeAnc
import utils
import config as ttconf

import numpy as np
from Bio import AlignIO, Phylo
import datetime
from scipy.interpolate import interp1d
import matplotlib.pyplot as plt
import matplotlib as mpl
import json
import copy
from scipy import optimize as sciopt

class TreeTime(TreeAnc, object):
    """
    TreeTime is the main class to perform the optimization of the node
    positions  given the temporal constraints of (some) nodes and leaves.
    
    The optimization workflow includes the inferrence of the ancestral sequencres 
    using Fitch method or maximum-likelihood (ML), followed by the free optimization 
    of the branch lengths with maximum-likelihood method. After the optimization 
    is done, the nodes with date-time information are arranged along the time axis, 
    the appropriate conversion btween the branch lengths units and the date-time units
    is found. Then, for each internal node, we compute the the probability distribution
    of the node's location conditional on the fixed location of the leaves, which 
    have temporal information. In the end, the most probable location of the internal nodes 
    is converted to the time of the internal node. 
    """

    def __init__(self):
        super(TreeTime, self).__init__()
        self.date2dist = None  # we do not know anything about the conversion
        self.tree_file = ""
        self.max_diam = 0.0

    @property
    def average_branch_len(self):
        """
        Compute the average branch length of the binary tree.
        **NOTE** There is no check for the tree to be binary, sofor real trees 
        this is only an approximation (due to the possible multiple
        mergers). However, we need this value only to estimate the scale 
        of the branch-lenghts, so in every reals situation, the precision is 
        enough.
        """
        tot_branches = (self.tree.count_terminals() -1)* 2 # for binary tree !
        tot_len = self.tree.total_branch_length ()
        return tot_len/tot_branches

    def reroot_to_oldest(self):
        """
        Set the root of the tree to the oldest node. 
        """
        def raw_date(node):
            if not hasattr(node, 'raw_date') or node.raw_date is None:
                return 0
            return node.raw_date

        self.tree.root_with_outgroup(sorted(self.tree.get_terminals(), key=raw_date)[-1])
        self.tree.ladderize()
        og = self.tree.root.clades[0]
        self.tree.root.clades[1].branch_length += og.branch_length
        og.branch_length = self.one_mutation
        self.tree.root.branch_length = self.one_mutation
        self.tree.root.raw_date = None
        # fix tree lengths, etc
        self.set_additional_tree_params()

    def init_date_constraints(self, gtr, slope=None):
        """
        Get the conversion coefficients between the dates and the branch
        lengths as they are used in ML computations. The conversion formula is
        assumed to be 'length = k*raw_date + b'. For convenience, these
        coefficients as well as regression parameters are stored in the
        dates2dist object.

        Note: that tree must have dates set to all nodes before calling this
        function. (This is accomplished by calling load_dates func).
        """
        if slope is None:
            self.date2dist = utils.DateConversion.from_tree(self.tree)
            self.max_diam = self.date2dist.intersept
        else:
            dc = utils.DateConversion()
            dc.slope = slope
            min_raw_date = ttconf.BIG_NUMBER
            max_raw_date = -ttconf.BIG_NUMBER
            self.max_diam = 0.0
            for t in self.tree.get_terminals():
                # NOTE: raw_date is time before present in days
                if hasattr(t, 'raw_date') and t.raw_date is not None:
                    if t.raw_date < min_raw_date:
                        min_raw_date = t.raw_date
                    if t.raw_date > max_raw_date:
                        max_raw_date = t.raw_date
                        self.max_diam = t.dist2root
            if min_raw_date == ttconf.BIG_NUMBER:
                print ("Warning! cannot set the minimal raw date. using today")
                min_raw_date = 0.0
            if self.max_diam == 0.0:
                print ("Error! cannot set the intersept for the date2dist conversion!"
                    "Cannot read tree diameter")
                return
            dc.intersept = self.max_diam - slope * min_raw_date
            self.date2dist = dc
        # set the None  for the date-related attributes in the internal nodes. 
        # make interpolation objects for the branches
        self._ml_t_init(gtr)

    def _make_branch_len_interpolator(self, node, gtr, n=ttconf.BRANCH_GRID_SIZE):
        """
        Makes an interpolation object for propability of branch length. Builds 
        an adaptive grid with n points, fine arround the optimal branch lengths, 
        and more sparse at the tails. This method does **not**  require the 
        branch lengths to be at their optimal values, as it computes the optimal 
        lengths itself. The method, however **requires** the knowledge of the 
        sequences and/or sequence profiles for the node and its parent in order
        to cpmpute distance probability in the scope of the GTR models
        
        Args:
         - node(Phylo.Clade): tree node. The probability distribution fro the branch 
         from the node to its parent is to be  computed

         - gtr(GTR): GTR model of evolution. required to determine the probability 
         of the  two given sequences to be separated by time t (i.e. the branch 
        length have length t)
        
         - n(int): numbe of points in the branch length grid. 

        Returns: 
         - None. The node gets new attribute - the linear interpolation object 
         of the branch length probability distribution. 

        """
        # no need to optimize the root branch length
        if node.up is None:
            node.branch_neg_log_prob = None
            return None

        parent = node.up
        prof_p = parent.profile
        prof_ch = node.profile

        # optimal branch length
        obl = gtr.optimal_t(node.up.profile, node.profile) # not rotated profiles!

        if obl < np.min((1e-5, 0.1*self.one_mutation)): # zero-length

            grid = ttconf.MAX_BRANCH_LENGTH * (np.linspace(0, 1.0 , n)**2)

        else: # branch length is not zero

            sigma = obl #np.max([self.average_branch_len, obl])
            # from zero to optimal branch length 
            grid_left = obl * (1 - np.linspace(1, 0.0, n)**2)
            # from optimal branch length to the right (--> 3*branch lengths), 
            grid_right = obl + obl/100 + (3*sigma*(np.linspace(0, 1, n)**2))
            # far to the right (3*branch length ---> MAX_LEN), very sparse
            far_grid = grid_right.max() + obl/2 + ttconf.MAX_BRANCH_LENGTH*np.linspace(0, 1, n)**2

            grid = np.concatenate((grid_left,grid_right,far_grid))
            grid.sort() # just for safety 

        grid = np.concatenate((
            [ttconf.MIN_T, -ttconf.TINY_NUMBER],
            grid,
            [ttconf.MAX_T]))

        # log-probability of the branch len to be at this value
        logprob = np.concatenate([
            [0., 0.],
            [gtr.prob_t(prof_p, prof_ch, t_, return_log=True) for t_ in grid[2:-2]],
            [0., 0.]]) - ttconf.BRANCH_LEN_PENALTY * grid

        logprob[((0,1,-2,-1),)] = ttconf.MIN_LOG
        logprob *= -1.0

        # normalize the branch lengths prob distribution
        dt = np.diff(grid)
        tmp_prob = np.exp(-logprob)
        integral = np.sum(0.5*(tmp_prob[1:]+tmp_prob[:-1])*dt)

        node.branch_neg_log_prob = interp1d(grid, logprob+np.log(integral),
                                            kind='linear')
        
        # node gets new attribute
        return None

    def _ml_t_init(self, gtr):
        """
        Initialize the necessary attributes in all tree nodes, which are required 
        by the ML algorithm to compute the probablility distribution of hte nodes
        locations. These attributes include the distance from the nodes postions 
        to the present (in branch-lengths units), branch-lenghts interpolation 
        objects, and the probability distributions for the nodes which have the 
        date-time information (these are going to be delta-functions), and 
        set the sequence profiles in the eigenspace of the used GTR matrix.

        Args:
         - gtr(GTR): Evolutionary model. 
        """
        tree = self.tree

        if self.date2dist is None:
            print ("error - no date to dist conversion set. Run init_date_constraints and try once more.")
            return
        for node in tree.find_clades():
            # node is constrained
            if hasattr(node, 'raw_date') and node.raw_date is not None:
                # set the absolute time in branch length units
                # the abs_t zero is today, and the direction is to the past
                node.abs_t = node.raw_date * abs(self.date2dist.slope)
                node.msg_to_parent = utils.delta_fun(node.abs_t, return_log=True, normalized=False)
            # unconstrained node
            else:
                node.raw_date = None
                node.abs_t = None 
                # if there are no constraints - log_prob will be set on-the-fly
                node.msg_to_parent = None
            # make interpolation object for branch lengths
            self._make_branch_len_interpolator(node, gtr, n=ttconf.BRANCH_GRID_SIZE)
            # set the profiles in the eigenspace of the GTR matrix
            # in the following, we only use the prf_l and prf_r (left and right
            # profiles in the matrix eigenspace)
            self._set_rotated_profiles(node, gtr)

    def _convolve(self, src_neglogprob, src_branch_neglogprob, inverse_time):
        """
        Compute the convolution of parent (target) and child (source)
        nodes inverse log-likelihood distributions.
        Take the source node log-LH distribution, extracts its grid. Based on
        the brach length probability distrribution (also inverse log-LH), find
        approximate position of the target node. Make the grid for the target
        node, and for each point of this newly generated grid, compute the
        convolution over all possible positions of the source node.

        Args:

        - src_neglogprob (scipy.interpolate.interp1d): inverse log-LH
         distribution of the node to be integrated, represented as scipy
         interpolation object

        - src_branch_neglogprob(scipy.interpolate.interp1d): inverse log-LH
         distribution of the branch lenghts between the two nodes, represented
         as scipy interpolation object

         - inverse_time (bool): Whether the time should be inversed.
         True if we go from leaves to root (against absolute time scale), and
         the convolution is computed over positions of the child node.
         False if the messages are propagated from root towards leaves (the same
         direction as the absolute time axis), and the convolution is being
         computed over the position of the parent node

        """

        opt_source_pos = utils.min_interp(src_neglogprob)
        opt_branch_len = utils.min_interp(src_branch_neglogprob)
        if inverse_time:
            opt_target_pos = opt_source_pos + opt_branch_len # abs_t
        else:
            opt_target_pos = opt_source_pos - opt_branch_len

        # T
        target_grid = utils.make_node_grid(opt_target_pos)
        target_grid.sort() # redundant
        if hasattr(src_neglogprob, 'delta_pos'): # convolve with delta-fun
            x_axis = target_grid - src_neglogprob.delta_pos
            x_axis[x_axis < ttconf.MIN_T] = ttconf.MIN_T
            x_axis[x_axis > ttconf.MAX_T] = ttconf.MAX_T
            res_y = src_branch_neglogprob(x_axis)
            res = interp1d(target_grid, res_y, kind='linear')
        else: # convolve two different distributions
            pre_b = np.min(src_branch_neglogprob.y)
            pre_n = np.min(src_neglogprob.y)
            src_branch_neglogprob.y -= pre_b
            src_neglogprob.y -= pre_n
            res = utils.convolve(target_grid, src_neglogprob, src_branch_neglogprob)
            src_branch_neglogprob.y += pre_b
            src_neglogprob.y += pre_n
            res.y += pre_b
            res.y += pre_n
        return res

    def _ml_t_leaves_root(self):
        """
        Compute the probability distribution of the internal nodes positions by 
        propagating from the tree leaves towards the root. Note the result of 
        this opeation is the probability distributions of each internal node,  
        conditional on the leaves constraint of the subtree of the node. The exception
        is the root of the tree, as its subtree includes all the constrained leaves. 
        To compute the location probability distribution of the internal nodes, 
        the back-propagation is needed. 
        
        Args:
        
         - None: all requered parameters are pre-set as the node atributes at the 
         preparation step

        Returns: 
        
         - None: Every internal node is assigned the probability distribution as 
         the interpolation object and sends this distribution further towards the 
         root. 
        
        """

        print("Maximum likelihood tree optimization with temporal constraints:"
            " Propagating leaves -> root...")
        for node in self.tree.find_clades(order='postorder'):  # children first, msg to parents

            if node.is_terminal():
                continue # either have constraints, or will be optimized freely on the way back

            # children nodes with constraints
            msgs_from_clades = [self._convolve(clade.msg_to_parent,
                               clade.branch_neg_log_prob,
                               inverse_time=True)
                               for clade in node.clades if clade.msg_to_parent is not None]
            if len(msgs_from_clades) < 1:  # we need at least one constraint
                continue

            new_neglogprob = utils.multiply_dists(msgs_from_clades)
            node.msg_to_parent = new_neglogprob

    def _ml_t_root_leaves(self):
        """
        Given the location probability distribution, computed by the propagation 
        from leaves to root, set the root most-likely location. Estimate the 
        tree likelihood. Report the root location  probability distribution
        message towards the leaves. For each internal node, compute the final 
        location probability distribution based on the pair of messages (from the 
        leaves and from the root), and find the most likely position of the 
        internal nodes and finally, convert it to the date-time information
        
        Args:

        - None: all the requires parameters are pre-set in the previous steps. 

        Returns:
         - None: all the internal nodes are assigned with the date-time information
         the probability distribution of their locations. The branch lengths are 
         being corrected according to the nodes locations. 

        """

        print("Maximum likelihood tree optimization with temporal constraints:"
            " Propagating root -> leaves...")
        for node in self.tree.find_clades(order='preorder'):  # ancestors first, msg to children
            if not hasattr(node, "msg_to_parent"):
                print ("ERROR: node has no log-prob interpolation object! "
                    "Aborting.")
            if node.up is None:  # root node
                node.total_prob = utils.delta_fun(utils.min_interp(node.msg_to_parent),return_log=True,normalized=False)
                #node.total_prefactor = node.msg_to_parent_prefactor
                #node.msg_from_root_prefactor = 0
                self._set_final_date(node)
                continue

            if node.msg_to_parent is not None: # constrained terminal
                                              # and all internal nodes


                if not hasattr(node.up.total_prob ,'delta_pos'):
                    print ("Cannot infer the position of the node: the position "
                        "of the parent is not delta function")
                    continue

                node_grid = node.up.total_prob.delta_pos - node.branch_neg_log_prob.x
                node_grid[node_grid < ttconf.MIN_T/2] = ttconf.MIN_T
                node_grid[node_grid > ttconf.MAX_T/2] = ttconf.MAX_T
                node.msg_from_root = interp1d(node_grid, node.branch_neg_log_prob.y, kind='linear')

                final_prob = utils.multiply_dists((node.msg_from_root, node.msg_to_parent))

                if utils.min_interp(final_prob) > node.up.abs_t:
                    # must never happen, just for security
                    node.total_prob = utils.delta_fun(node.up.abs_t, return_log=True, normalized=False)
                    print ("Error in the node probability distribution: the "
                        "node's location is earlier than previous!. ")
                else:
                    node.total_prob = utils.delta_fun(utils.min_interp(final_prob),
                        return_log=True, normalized=False)

            else: # unconstrained terminal nodes
                node_grid = node.up.total_prob.delta_pos - node.branch_neg_log_prob.x
                node_grid[node_grid < ttconf.MIN_T/2] = ttconf.MIN_T
                node_grid[node_grid > ttconf.MAX_T/2] = ttconf.MAX_T
                node.msg_from_root = interp1d(node_grid, node.branch_neg_log_prob.y, kind='linear')
                final_prob = utils.multiply_dists((node.msg_from_root, node.msg_to_parent))
                node.msg_from_root = msg_from_root

                node.total_prob = utils.delta_fun(utils.min_interp(node.msg_from_root),
                        return_log=True, normalized=False)

            self._set_final_date(node)

    def _set_final_date(self, node):
        """
        Given the location of the node in branch lengths units, convert it to the 
        date-time information. 

        Args:
         - node(Phylo.Clade): tree node. NOTE the node should have the abs_t attribute 
         to have a valid value. This is automatically taken care of in the 
         procedure to get the node location probability distribution. 

        """
        node.abs_t = utils.min_interp(node.total_prob)
        if node.up is not None:
            node.branch_length = node.up.abs_t - node.abs_t
            node.dist2root = node.up.dist2root + node.branch_length
        else:
            node.branch_length = self.one_mutation
            node.dist2root = 0.0

        node.date = self.date2dist.get_date(node.abs_t)

    def ml_t(self, gtr):
        """
        Perform the maximum-likelihood -- based optimization of the tree with temporal 
        constraints of (some) internal nodes. 
        
        Args: 

         - gtr(GTR): general time-revesible model, which is required for the post-
         optimization of the ancestral sequences. NOTE that GTR is not required 
         in theprocess of the optimization itself, since all the distance-based 
         parameters are pre-set at the preparation steps (namely, the branch lengths 
        interpolation objects).
        
        Returns: 

         - None: Updates the tree, its branch lengths and information about the
         internal nodes. 
        """
        #  propagate messages up
        self._ml_t_leaves_root()

        #  propagate messages down - reconstruct node positions
        # self._ml_t_root_leaves_tmp()
        self._ml_t_root_leaves()
        self._ml_anc(gtr)
        print ("Done tree optimization.")

    def _set_rotated_profiles(self, node, gtr):
        """
        Set sequence and its profiles in the eigenspace of the transition
        matrix.
        """
        node.prf_r = node.profile.dot(gtr.v)
        node.prf_l = (gtr.v_inv.dot(node.profile.T)).T

    def _score_branch(self, node):
        """
        Auxilliary function to see how well is the particular branch optimized 
        (how far it is from its roptimal value)
        """
        cmap = mpl.cm.get_cmap ()
        def dev(n):
            sign = np.sign(node.branch_length - utils.opt_branch_len(node))
            opt_bl = sign * abs(node.branch_neg_log_prob(utils.opt_branch_len(n)) - node.branch_neg_log_prob(node.branch_length))

            return opt_bl #(n.branch_length - opt_bl) # neg_log_prob(tmp_eps+n.branch_length) - n.branch_neg_log_prob(n.branch_length))/tmp_eps

        node._score = dev(node)
        #print (node._score)
        return None

    def _score_branches(self):
        """
        Set score to the branch. The score is how far is the branch length from
        its optimal value
        """

        all_scores = []
        for n in self.tree.find_clades():
            if n.up is not None:
                self._score_branch(n)
                all_scores.append(n._score)

        score_min, score_max = min(all_scores), max(all_scores)
        abs_max = max(np.abs(all_scores))
        from matplotlib import cm
        for n in self.tree.find_clades():
            if n.up is not None:
                n.color = list(map(lambda x:int(255*x), cm.jet((n._score+abs_max)/(2*score_max))[:3]))

    def log_lh(self, node):
        """
        Get log-likelihood of the tree given the constrained leaves.
        """
        if hasattr(node, 'lh_prefactor') and hasattr(node, 'msg_to_parent_prefactor'):
            return -node.root.msg_to_parent_prefactor + node.lh_prefactor.sum()
        else:
            return ttconf.MIN_LOG


    def resolve_polytomies(self, gtr):
        """
        Resolve the polytomies on the tree given the joining algorithm opt.
        The function scans the tree, resolves polytomies in case there are any,
        and re-optimizes the tree with new topology.

        Args:
         - gtr(TreeAnc.GTR): evolutionary model

         - opt(callable): function, which converts the node with polytomies into
         the binary tree. Use one of the standard functions (e.g.
         ladderize_node_polytomies or optimize_node_polytomies), or provide
         your own

         - opt_args(tuple): arguments for the optimization algorithm opt.
        """
        for n in self.tree.find_clades():
            if len(n.clades) > 3: self._poly(n, gtr)

        self.optimize_branch_len(gtr)
        self.optimize_seq_and_branch_len(gtr, prune_short=False)
        self._ml_t_init(gtr)
        self.ml_t(gtr)
        self._ml_anc(gtr)
        self.tree.ladderize()

    def _poly(self, clade, gtr, verbose=10):

        """
        Function to resolve polytomies for a given parent node. If the number of the 
        direct decendants is less than three (not a polytomy), does nothing. 
        Otherwise, for each pair of nodes, assess the possible LH increase which could be 
        gained by merging the two nodes. The increase in the LH is basically the 
        tradeoff between the gain of the LH due to the changing the branch lenghts towardsthe optimal 
        values and the decrease due to the introduction of the new branch with zero
        optimal length. After the cost gains been determined, 
        """

        # TODO coefficient fromt the gtr
        zero_branch_slope = self.one_mutation / 0.8

        def _c_gain(t, n1, n2, parent):
            """
            cost gain if nodes n1, n2 are joined and their parent is placed at time t

            cost gain = (LH loss now) - (LH loss when placed at time t)
                      = [LH(opt) - LH(now)] - [LH(opt) - LH(t)] approx.=
                      approx.= LH(branch_len(now)) - LH (branch_len(t))

            """
            cg2 = n2.branch_neg_log_prob(parent.abs_t - n2.abs_t ) - n2.branch_neg_log_prob (t - n2.abs_t)
            cg1 = n1.branch_neg_log_prob(parent.abs_t - n1.abs_t ) - n1.branch_neg_log_prob (t - n1.abs_t)
            cg_new = - zero_branch_slope * (parent.abs_t - t) # loss in LH due to the new branch
            return -(cg2+cg1+cg_new)

        def cost_gain(n1, n2, parent):
            """
            cost gained if teh two nodes would have been connected.
            """
            cg = sciopt.minimize_scalar(_c_gain,
                    bounds=[np.max(n1.abs_t,n2.abs_t), parent.abs_t],
                    method='Bounded',args=(n1,n2, parent))
            return cg['x'], - cg['fun']

        stretched = [c for c  in clade.clades if utils.opt_branch_len(c) < c.branch_length]
        compressed = [c for c in clade.clades if c not in stretched]

        if verbose>5:
            print (stretched)

        LH = 0.0

        while len(stretched) > 1:
            print (len(stretched))

            # max possible gains of the cost when connecting the nodes:
            # this is only a rough approximation because it assumes the new node positions
            # to be optimal
            cost_gains = np.array([[cost_gain(n1,n2, clade) for n1 in stretched]for n2 in stretched])
            new_positions = cost_gains[:,:,0]
            cost_gains = cost_gains[:,:,1]

            np.fill_diagonal(cost_gains, 0.0)

            idxs = np.unravel_index(cost_gains.argmax(),cost_gains.shape)
            assert (idxs[0] != idxs[1])
            n1, n2 = stretched[idxs[0]], stretched[idxs[1]]
            print (n1,n2)
            print ("Delta-LH = " + str(cost_gains[idxs].round(3)))
            LH += cost_gains[idxs]

            new_node = Phylo.BaseTree.Clade()

            # fix positions and branch lengths
            new_node.abs_t = new_positions[idxs] # (n1.abs_t + tree.opt_branch_len(n1) + n2.abs_t + tree.opt_branch_len(n2))/2
            new_node.branch_length = clade.abs_t - new_node.abs_t
            new_node.clades = [n1,n2]
            n1.branch_length = new_node.abs_t - n1.abs_t
            n2.branch_length = new_node.abs_t - n2.abs_t

            # set parameters for the new node
            new_node.up = clade
            n1.up = new_node
            n2.up = new_node
            new_node.sequence = clade.sequence
            new_node.profile = clade.profile
            new_node.mutations = []
            self._make_branch_len_interpolator(new_node, gtr, n=36)
            clade.clades.remove(n1)
            clade.clades.remove(n2)
            clade.clades.append(new_node)

            # and modify stretched array for the next loop
            # because stretched  == clade.clades, we do not need this
            stretched.remove(n1)
            stretched.remove(n2)
            stretched.append(new_node)

        return LH

    def print_lh(self):
        """
        Print the total likelihood of the tree given the constrained leaves
        """
        s_lh = -self.tree.sequence_LH
        t_lh = self.tree.root.msg_to_parent.y.min()

        print ("###  Tree Likelihood  ###\n"
                " Seq log-LH:      {0}\n"
                " Temp.Pos log-LH: {1}\n"
                " Total log-LH:    {2}\n"
               "#########################".format(s_lh, t_lh, s_lh+t_lh))

    def total_LH(self):
        s_lh = self.tree.sequence_LH
        t_lh = -self.tree.root.msg_to_parent.y.min()
        return s_lh+t_lh

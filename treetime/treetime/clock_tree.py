import utils
import numpy as np
import config as ttconf
from treeanc import TreeAnc
from distribution import Distribution
from branch_len_interpolator import BranchLenInterpolator
from node_interpolator import NodeInterpolator
import collections

class ClockTree(TreeAnc):
    """
    ClockTree is the main class to perform the optimization of the node
    positions given the temporal constraints of (some) leaves.

    The optimization workflow includes the inference of the ancestral sequences
    and branch length optimization using TreeAnc. After the optimization
    is done, the nodes with date-time information are arranged along the time axis,
    the conversion between the branch lengths units and the date-time units
    is determined. Then, for each internal node, we compute the the probability distribution
    of the node's location conditional on the fixed location of the leaves, which
    have temporal information. In the end, the most probable location of the internal nodes
    is converted to the most likely time of the internal nodes.
    """

    def __init__(self,  dates=None, debug=False, *args, **kwargs):
        super(ClockTree, self).__init__(*args, **kwargs)
        if dates is None:
            raise("ClockTree requires date contraints!")

        self.debug=debug

        self.date_dict = dates
        self.date2dist = None  # we do not know anything about the conversion
        self.n_integral = ttconf.NINTEGRAL
        self.rel_tol_prune = ttconf.REL_TOL_PRUNE
        self.rel_tol_refine = ttconf.REL_TOL_REFINE
        self.merger_rate_default = ttconf.BRANCH_LEN_PENALTY

        for node in self.tree.find_clades():
            if node.name in self.date_dict:
                node.numdate_given = self.date_dict[node.name]
                node.bad_branch = False
            else:
                if node.is_terminal():
                    node.bad_branch = True
                node.numdate_given = None


    @property
    def date2dist(self):
        return self._date2dist

    @date2dist.setter
    def date2dist(self, val):
        if val is None:
            self._date2dist = None
        else:
            self.logger("ClockTime.date2dist: Setting new date to branchlength conversion. slope=%f, R^2=%.4f"%(val.slope, val.r_val**2), 2)
            self._date2dist = val


    def init_date_constraints(self, ancestral_inference=False, slope=None, **kwarks):
        """
        Get the conversion coefficients between the dates and the branch
        lengths as they are used in ML computations. The conversion formula is
        assumed to be 'length = k*numdate_given + b'. For convenience, these
        coefficients as well as regression parameters are stored in the
        dates2dist object.

        Note: that tree must have dates set to all nodes before calling this
        function. (This is accomplished by calling load_dates func).

        Params:
            ancestral_inference: bool -- whether or not to reinfer ancestral sequences
                                 done by default when ancestral sequences are missing

        """
        self.logger("ClockTree.init_date_constraints...",2)

        if ancestral_inference or (not hasattr(self.tree.root, 'sequence')):
            self.infer_ancestral_sequences('ml',sample_from_profile='root',**kwarks)

        # set the None  for the date-related attributes in the internal nodes.
        # make interpolation objects for the branches
        self.logger('ClockTree.init_date_constraints: Initializing branch length interpolation objects...',3)
        for node in self.tree.find_clades():
            if node.up is None:
                node.branch_length_interpolator = None
            else:
                # copy the merger rate and gamma if they are set
                if hasattr(node,'branch_length_interpolator') and node.branch_length_interpolator is not None:
                    gamma = node.branch_length_interpolator.gamma
                    merger_rate = node.branch_length_interpolator.merger_rate
                else:
                    gamma = 1.0
                    merger_rate = self.merger_rate_default
                node.branch_length_interpolator = BranchLenInterpolator(node, self.gtr, one_mutation=self.one_mutation)
                node.branch_length_interpolator.merger_rate = merger_rate
                node.branch_length_interpolator.gamma = gamma
        self.date2dist = utils.DateConversion.from_tree(self.tree, slope)

        # make node distribution objects
        for node in self.tree.find_clades():
            # node is constrained
            if hasattr(node, 'numdate_given') and node.numdate_given is not None:
                # set the absolute time before present in branch length units
                if not np.isscalar(node.numdate_given):
                    node.numdate_given = np.array(node.numdate_given)
                node.time_before_present = self.date2dist.get_time_before_present(node.numdate_given)
                if hasattr(node, 'bad_branch') and node.bad_branch==True:
                    self.logger("ClockTree.init_date_constraints -- WARNING: Branch is marked as bad"
                                ", excluding it from the optimization process"
                                " Will be optimized freely", 4, warn=True)
                    # if there are no constraints - log_prob will be set on-the-fly
                    node.msg_from_children = None
                else:
                    if np.isscalar(node.numdate_given):
                        node.msg_from_children = NodeInterpolator.delta_function(node.time_before_present, weight=1)
                    else:
                        node.msg_from_children = NodeInterpolator(node.time_before_present,
                                                              np.ones_like(node.time_before_present), is_log=False)

            else: # node without sampling date set
                node.numdate_given = None
                node.time_before_present = None
                # if there are no constraints - log_prob will be set on-the-fly
                node.msg_from_children = None


    def make_time_tree(self, do_marginal=False, **kwargs):
        '''
        use the date constraints to calculate the most likely positions of
        unconstraint nodes.
        '''
        self.logger("ClockTree: Maximum likelihood tree optimization with temporal constraints:",1)
        self.init_date_constraints(**kwargs)

        self._ml_t_joint()

        if do_marginal:
            self._ml_t_marginal()

        #self._set_final_dates()
        self.convert_dates()


    def _ml_t_joint(self):
        """
        Compute the joint probability distribution of the internal nodes positions by
        propagating from the tree leaves towards the root. Given the probability distributions,
        reconstruct the maximum-likelihood positions of the internal root by propagating
        from the root to the leaves. The result of this operation is the time_before_present
        value, which is the position of the node, expressed in the units of the
        branch length, and scaled from the present-day. The value is assigned to the
        corresponding attribute of each node of the tree.

        Args:

         - None: all required parameters are pre-set as the node attributes during
           tree preparation

        Returns:

         - None: Every internal node is assigned the probability distribution in form
           of an interpolation object and sends this distribution further towards the
           root.

        """

        def _cleanup():
            for node in self.tree.find_clades():
                del node.joint_pos_Lx
                del node.joint_pos_Cx


        self.logger("ClockTree - Joint reconstruction:  Propagating leaves -> root...", 2)
        # go through the nodes from leaves towards the root:
        for node in self.tree.find_clades(order='postorder'):  # children first, msg to parents

            if node.is_terminal(): # terminal nodes

                # initialize the Lx for the terminal nodes:
                if node.msg_from_children.is_delta: # there is a time contraint
                    # subtree probability given the position of the parent node
                    # position of the parent node is given by the Lx.x
                    # probablity of the subtree (consiting of one terminal node)
                    # is given by the Lx.y
                    node.joint_pos_Lx = Distribution.shifted_x(node.branch_length_interpolator, node.msg_from_children.peak_pos)
                    node.joint_pos_Cx = Distribution (node.msg_from_children.peak_pos + node.branch_length_interpolator.x, node.branch_length_interpolator.x) # only one value

                # terminal node has inprecise date constraint
                elif node.msg_from_children is not None:

                    res, res_t = NodeInterpolator.convolve(node.msg_from_children,
                                node.branch_length_interpolator,
                                max_or_integral='max',
                                inverse_time=True,
                                n_integral=self.n_integral,
                                rel_tol=self.rel_tol_refine)

                    res._adjust_grid(rel_tol=self.rel_tol_prune)

                    node.joint_pos_Lx = res
                    node.joint_pos_Cx = res_t

                else:
                    # node send "No message" - its position is assumed from the
                    # position of the parent node
                    node.joint_pos_Lx = None
                    node.joint_pos_Cx = None

            elif node.up is not None: # all internal nodes, except root

                # subtree likelihood given the node's position Ta:
                msg_from_children = Distribution.multiply([child.joint_pos_Lx for child in node.clades
                                                        if child.joint_pos_Lx is not None])

                res, res_t = NodeInterpolator.convolve(msg_from_children,
                                node.branch_length_interpolator,
                                max_or_integral='max',
                                inverse_time=True,
                                n_integral=self.n_integral,
                                rel_tol=self.rel_tol_refine)

                res._adjust_grid(rel_tol=self.rel_tol_prune)

                node.joint_pos_Lx = res
                node.joint_pos_Cx = res_t

            else: # root node is processed separately (has no condition on the parent)

                # probability of the subtree given the root position is defined by the
                # msg_from_children.x
                msg_from_children = Distribution.multiply([child.joint_pos_Lx for child in node.clades
                                                        if child.joint_pos_Lx is not None])

                msg_from_children._adjust_grid(rel_tol=self.rel_tol_prune)

                root_pos = msg_from_children.peak_pos
                joint_pos_LH = msg_from_children.peak_val

                # set root position and joint likelihood of the tree
                self.tree.joint_pos_LH = joint_pos_LH
                node.time_before_present = root_pos
                node.joint_pos_Lx = msg_from_children
                node.joint_pos_Cx = None

        # go through the nodes from root towards the leaves:
        self.logger("ClockTree - Joint reconstruction:  Propagating root -> leaves...", 2)
        for node in self.tree.find_clades(order='preorder'):  # children first, msg to parents

            if node.up is None: # root node
                continue # the position is already set on the previuos step

            if node.joint_pos_Cx is None:# no constraints or branch is bad - reconstruct from the branch len interpolator
                node.time_before_present = node.up.time_before_present - node.branch_length_interpolator.peak_pos
                node.branch_length = node.branch_length_interpolator.peak_pos

            elif isinstance(node.joint_pos_Cx, Distribution):
                # Cx is the distribution, so we reconstruct the position from the information passed
                # by its parent

                # NOTE the Lx distribution is the likelihood, given the position of the parent
                # (Lx.x = parent position, Lx.y = LH of the node_pos given Lx.x,
                # the value of the node_pos is given by the node.Cx array)
                subtree_LH = node.joint_pos_Lx(node.up.time_before_present)
                node.branch_length = node.joint_pos_Cx(node.up.time_before_present)
                node.time_before_present = node.up.time_before_present - node.branch_length
                node.clock_length = node.branch_length

            else: # node positions has "delta-function" constraint
                subtree_LH = node.joint_pos_Lx(node.up.time_before_present)
                node.branch_length = node.joint_pos_Cx
                node.time_before_present = node.time_before_present = node.up.time_before_present - node.joint_pos_Cx

            # just sanity check, should never happen:
            if node.branch_length < 0 or node.time_before_present < 0:
                print (node.time_before_present, node.branch_length)
                if self.debug:
                    import ipdb; ipdb.set_trace()

        # cleanup, if required
        if not self.debug:
            _cleanup()

    def _ml_t_marginal(self):
        """
        Compute the marginal probability distribution of the internal nodes positions by
        propagating from the tree leaves towards the root. The result of
        this operation are the probability distributions of each internal node,
        conditional on the constraints on all leaves of the tree, which have sampling dates.
        The probability distributions are set as marginal_pos_LH attributes to the nodes.

        Args:

         - None: all required parameters are pre-set as the node attributes during
           tree preparation

        Returns:

         - None: Every internal node is assigned the probability distribution in form
           of an interpolation object and sends this distribution further towards the
           root.

        """

        def _cleanup():
            for node in self.tree.find_clades():
                del node.marginal_pos_Lx
                del node.msg_from_children
                del node.msg_from_parent
                #del node.marginal_pos_LH


        self.logger("ClockTree - Marginal reconstruction:  Propagating leaves -> root...", 2)
        # go through the nodes from leaves towards the root:
        for node in self.tree.find_clades(order='postorder'):  # children first, msg to parents

            if node.is_terminal(): # terminal nodes

                # initialize the Lx for the terminal nodes:
                if node.msg_from_children.is_delta: # there is a time contraint
                    # subtree probability given the position of the parent node
                    # position of the parent node is given by the Lx.x
                    # probablity of the subtree (consiting of one terminal node)
                    # is given by the Lx.y
                    node.marginal_pos_Lx = Distribution.shifted_x(node.branch_length_interpolator, node.msg_from_children.peak_pos)

                elif node.msg_from_children is not None: # the distribution attached as message from the subtree
                    res, res_t = NodeInterpolator.convolve(node.msg_from_children,
                                node.branch_length_interpolator,
                                max_or_integral='integral',
                                n_integral=self.n_integral,
                                rel_tol=self.rel_tol_refine)
                    res._adjust_grid(rel_tol=self.rel_tol_prune)

                    node.marginal_pos_Lx = res

                else:
                    # node send "No message" - its position is assumed from the
                    # position of the parent node
                    node.marginal_pos_Lx = None

            elif node.up is not None: # all internal nodes, except root

                # subtree likelihood given the node's position Ta:
                msg_from_children = Distribution.multiply([child.marginal_pos_Lx for child in node.clades
                                                        if child.marginal_pos_Lx is not None])

                node.msg_from_children = msg_from_children

                res, res_t = NodeInterpolator.convolve(msg_from_children,
                                node.branch_length_interpolator,
                                max_or_integral='integral',
                                n_integral=self.n_integral,
                                rel_tol=self.rel_tol_refine)

                res._adjust_grid(rel_tol=self.rel_tol_prune)

                node.marginal_pos_Lx = res

            else: # root node is processed separately (has no condition on the parent)


                # probability of the subtree given the root position is defined by the
                # msg_from_children.x
                msg_from_children = Distribution.multiply([child.marginal_pos_Lx for child in node.clades
                                                        if child.marginal_pos_Lx is not None])

                msg_from_children._adjust_grid(rel_tol=self.rel_tol_prune)

                node.msg_from_children = msg_from_children

                root_pos = msg_from_children.peak_pos
                marginal_tree_LH = msg_from_children.peak_val

                node.marginal_pos_Lx = msg_from_children
                node.marginal_pos_LH = msg_from_children
                self.tree.positional_LH = marginal_tree_LH

        self.logger("ClockTree - Marginal reconstruction:  Propagating root -> leaves...", 2)
        for node in self.tree.find_clades(order='preorder'):

            ## The root node
            if node.up is None:
                node.msg_from_parent = None # nothing beyond the root
                continue

            # unconstrained terminal node. Set the position from the optimal branch length
            elif node.marginal_pos_Lx is None:
                node.msg_from_parent = None
                continue

            # all other cases (All internal nodes + unconstrained terminals)
            else:
                parent = node.up
                # messages from the complementary subtree (iterate over all sister nodes)
                complementary_msgs = [sister.marginal_pos_Lx for sister in parent.clades
                                            if sister != node]

                # if parent itself got smth from the root node, include it
                if parent.msg_from_parent is not None:
                        complementary_msgs.append(parent.msg_from_parent)

                msg_parent_to_node = NodeInterpolator.multiply(complementary_msgs)
                msg_parent_to_node._adjust_grid(rel_tol=self.rel_tol_prune)

                # integral message, which delievers to the node the positional information
                # from the complementary subtree
                res, res_t = NodeInterpolator.convolve(msg_parent_to_node, node.branch_length_interpolator,
                                                    max_or_integral='integral',
                                                    inverse_time=False, n_integral=self.n_integral,
                                                    rel_tol=self.rel_tol_refine)

                node.msg_from_parent = res

                node.marginal_pos_LH = NodeInterpolator.multiply((node.msg_from_parent, node.marginal_pos_Lx))

                self.logger('ClockTree._ml_t_root_to_leaves: computed convolution'
                                ' with %d points at node %s'%(len(res.x),node.name),4)

                if self.debug:
                        tmp = np.diff(res.y-res.peak_val)
                        nsign_changed = np.sum((tmp[1:]*tmp[:-1]<0)&(res.y[1:-1]-res.peak_val<500))
                        if nsign_changed>1:
                            import matplotlib.pyplot as plt
                            plt.ion()
                            plt.plot(res.x, res.y-res.peak_val, '-o')
                            plt.plot(res.peak_pos - node.branch_length_interpolator.x,
                                     node.branch_length_interpolator.y-node.branch_length_interpolator.peak_val, '-o')
                            plt.plot(msg_parent_to_node.x,msg_parent_to_node.y-msg_parent_to_node.peak_val, '-o')
                            plt.ylim(0,100)
                            plt.xlim(-0.01, 0.01)
                            import ipdb; ipdb.set_trace()

        if not self.debug:
            _cleanup()

        return


    def convert_dates(self):
        from datetime import datetime, timedelta
        now = utils.numeric_date()
        for node in self.tree.find_clades():
            years_bp = self.date2dist.get_date(node.time_before_present)
            if years_bp < 0:
                if not hasattr(node, "bad_branch") or node.bad_branch==False:
                    self.logger("ClockTree.convert_dates -- WARNING: The node is later than today, but it is not"
                        "marked as \"BAD\", which indicates the error in the "
                        "likelihood optimization.",4 , warn=True)
                else:
                    self.logger("ClockTree.convert_dates -- WARNING: node which is marked as \"BAD\" optimized "
                        "later than present day",4 , warn=True)

            node.numdate = now - years_bp

            # set the human-readable date
            days = 365.25 * (node.numdate - int(node.numdate))
            year = int(node.numdate)
            try:
                n_date = datetime(year, 1, 1) + timedelta(days=days)
                node.date = datetime.strftime(n_date, "%Y-%m-%d")
            except:
                # this is the approximation
                n_date = datetime(1900, 1, 1) + timedelta(days=days)
                node.date = str(year) + "-" + str(n_date.month) + "-" + str(n_date.day)


    def branch_length_to_years(self):
        self.logger('ClockTree.branch_length_to_years: setting node positions in units of years', 2)
        if not hasattr(self.tree.root, 'numdate'):
            self.logger('ClockTree.branch_length_to_years: infer ClockTree first', 2,warn=True)
        self.tree.root.branch_length = 1.0
        for n in self.tree.find_clades(order='preorder'):
            if n.up is not None:
                n.branch_length = n.numdate - n.up.numdate


if __name__=="__main__":
    import matplotlib.pyplot as plt
    import seaborn as sns
    plt.ion()
    base_name = 'data/H3N2_NA_allyears_NA.20'
    #root_name = 'A/Hong_Kong/JY2/1968|CY147440|1968|Hong_Kong||H3N2/8-1416'
    root_name = 'A/New_York/182/2000|CY001279|02/18/2000|USA|99_00|H3N2/1-1409'
    with open(base_name+'.metadata.csv') as date_file:
        dates = {}
        for line in date_file:
            try:
                name, date = line.strip().split(',')
                dates[name] = float(date)
            except:
                continue

    from Bio import Phylo
    tree = Phylo.read(base_name + ".nwk", 'newick')
    tree.root_with_outgroup([n for n in tree.get_terminals() if n.name==root_name][0])
    myTree = ClockTree(gtr='Jukes-Cantor', tree = tree,
                        aln = base_name+'.fasta', verbose = 6, dates = dates)

    myTree.optimize_seq_and_branch_len(prune_short=True)
    # fix slope -- to test
    myTree.make_time_tree(slope=0.003)

    plt.figure()
    x = np.linspace(0,0.05,100)
    leaf_count=0
    for node in myTree.tree.find_clades(order='postorder'):
        if node.up is not None:
            plt.plot(x, node.branch_length_interpolator.prob_relative(x))
        if node.is_terminal():
            leaf_count+=1
            node.ypos = leaf_count
        else:
            node.ypos = np.mean([c.ypos for c in node.clades])
    plt.yscale('log')
    plt.ylim([0.01,1.2])

    fig, axs = plt.subplots(2,1, sharex=True, figsize=(8,12))
    x = np.linspace(-0.1,0.05,1000)+ myTree.tree.root.time_before_present
    Phylo.draw(tree, axes=axs[0], show_confidence=False)
    offset = myTree.tree.root.time_before_present + myTree.tree.root.branch_length
    cols = sns.color_palette()
    depth = myTree.tree.depths()
    for ni,node in enumerate(myTree.tree.find_clades()):
        if (not node.is_terminal()):
            axs[1].plot(offset-x, node.marginal_lh.prob_relative(x), '-', c=cols[ni%len(cols)])
            axs[1].plot(offset-x, node.joint_lh.prob_relative(x), '--', c=cols[ni%len(cols)])
        if node.up is not None:
            x_branch = np.linspace(depth[node]-2*node.branch_length-0.005,depth[node],100)
            axs[0].plot(x_branch, node.ypos - 0.7*node.branch_length_interpolator.prob_relative(depth[node]-x_branch), '-', c=cols[ni%len(cols)])
    axs[1].set_yscale('log')
    axs[1].set_ylim([0.01,1.2])
    axs[0].set_xlabel('')
    plt.tight_layout()

    myTree.branch_length_to_years()
    Phylo.draw(myTree.tree)
    plt.xlim(myTree.tree.root.numdate-1,
             np.max([x.numdate for x in myTree.tree.get_terminals()])+1)

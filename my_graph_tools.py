from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from graph_nets import blocks
from graph_nets import graphs
from graph_nets import modules
from graph_nets import utils_np
from graph_nets import utils_tf

import my_graph_tools as mgt
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import sonnet as snt
import tensorflow as tf
import h5py
from progressbar import progressbar
from sklearn.preprocessing import normalize
import matplotlib.pyplot as plt

pi = np.pi
twopi = np.pi*2


# Defaults for below were 2 and 16
NUM_LAYERS = 2  # Hard-code number of layers in the edge/node/global models.
LATENT_SIZE = 16  # Hard-code latent layer sizes for demos.
DTG = 0.5
NTG = int(60*24/DTG)

def make_mlp_model(Lsize=LATENT_SIZE,Nlayer=NUM_LAYERS):
  """Instantiates a new MLP, followed by LayerNorm.

  The parameters of each new MLP are not shared with others generated by
  this function.

  Returns:
    A Sonnet module which contains the MLP and LayerNorm.
  """
  """
  # Version with regularization
  return snt.Sequential([
      snt.nets.MLP([Lsize] * Nlayer, activate_final=True, use_dropout=True,
                  regularizers={"w":tf.keras.regularizers.l2(l=0.01),
                                "b":tf.keras.regularizers.l2(l=0.01)
                        }
                  ),
      snt.LayerNorm()
  ])
  """
  return snt.Sequential([
      snt.nets.MLP([Lsize] * Nlayer, activate_final=True)
  ])


class MLPGraphIndependent(snt.AbstractModule):
  """GraphIndependent with MLP edge, node, and global models."""

  def __init__(self, name="MLPGraphIndependent"):
    super(MLPGraphIndependent, self).__init__(name=name)
    with self._enter_variable_scope():
      self._network = modules.GraphIndependent(
          edge_model_fn=make_mlp_model,
          node_model_fn=make_mlp_model,
          global_model_fn=make_mlp_model
          )

  def _build(self, inputs):
    return self._network(inputs)


class MLPGraphNetwork(snt.AbstractModule):
  """GraphNetwork with MLP edge, node, and global models."""

  def __init__(self, name="MLPGraphNetwork"):
    super(MLPGraphNetwork, self).__init__(name=name)
    with self._enter_variable_scope():
        self._network = \
            modules.GraphNetwork(make_mlp_model, make_mlp_model,
                make_mlp_model,
                global_block_opt={"use_edges":False,"use_nodes":False})
                #lambda:timecrement(NTG,disable=True),

  def _build(self, inputs):
    return self._network(inputs)


def get_empty_graph(nodeshape,edgeshape,glblshape,senders,receivers):
    dic = {
        "globals": np.zeros(glblshape,dtype=np.float),
        "nodes": np.zeros(nodeshape,dtype=np.float),
        "edges": np.zeros(edgeshape,dtype=np.float),
        "senders": senders,
        "receivers": receivers
    }
    return utils_tf.data_dicts_to_graphs_tuple([dic])


class GeoMLP(snt.AbstractModule):
    """For extracting the geographic dependencies of each location"""
    def __init__(self,init_graph,name="GeoMLP"):
        super(GeoMLP, self).__init__(name=name)
        nnode, nedge = init_graph.nodes.shape[0], init_graph.edges.shape[0]
        nnode_ft, nedge_ft, nglbl_ft = 5,7,9
        
        with self._enter_variable_scope():
            self.node_mlp = snt.nets.MLP([nnode_ft])
            self.edge_mlp = snt.nets.MLP([nedge_ft])
            self.glbl_mlp = snt.nets.MLP([nglbl_ft])
            self.geograph = get_empty_graph((nnode,nnode_ft),
                                            (nedge,nedge_ft),
                                            (1,nglbl_ft),
                                            init_graph.senders,
                                            init_graph.receivers
                                           )
    
    def _build(self, inputs):
        self.geograph = self.geograph.replace(
                    nodes=self.node_mlp(inputs.nodes),
                    edges=self.edge_mlp(inputs.edges),
                    globals=self.glbl_mlp(inputs.globals))
        return self.geograph



class EncodeProcessDecode(snt.AbstractModule):
    """Full encode-process-decode model.

    The model we explore includes three components:
    - An "Encoder" graph net, which independently encodes the edge, node, and
      global attributes (does not compute relations etc.). Uses an MLP to expand.
    - A "Core" graph net, which performs N rounds of processing (message-passing)
      steps. The input to the Core is the concatenation of the Encoder's output
      and the previous output of the Core (labeled "Hidden(t)" below, where "t" is
      the processing step).
    - A "Decoder" graph net, which independently decodes the edge, node, and
      global attributes (does not compute relations etc.), on each message-passing
      step.

                        Hidden(t)   Hidden(t+1)
                           |            ^
              *---------*  |  *------*  |  *---------*
              |         |  |  |      |  |  |         |
    Input --->| Encoder |  *->| Core |--*->| Decoder |---> Output(t)
              |         |---->|      |     |         |
              *---------*     *------*     *---------*
    """

    def __init__(self,
                 edge_output_size=None,
                 node_output_size=None,
                 global_output_size=None,
                 name="EncodeProcessDecode"):
        super(EncodeProcessDecode, self).__init__(name=name)
        self._encoder = MLPGraphIndependent()
        self._core = MLPGraphNetwork()
        self._decoder = MLPGraphIndependent()
        # Transforms the outputs into the appropriate shapes.
        if edge_output_size is None:
            edge_fn = None
        else:
            edge_fn = lambda: snt.Linear(edge_output_size, name="edge_output")
        if node_output_size is None:
            node_fn = None
        else:
            node_fn = lambda: snt.Linear(node_output_size, name="node_output")
        with self._enter_variable_scope():
            self._output_transform = \
                modules.GraphIndependent(edge_fn, node_fn)

    def _build(self, input_op, num_processing_steps):
        latent = self._encoder(input_op)
        latent0 = latent
        output_ops = []
        for _ in range(num_processing_steps):
            core_input = utils_tf.concat([latent0, latent], axis=1)
            latent = self._core(core_input)
            decoded_op = self._decoder(latent)
            output_ops.append(self._output_transform(decoded_op).replace(
                              globals=input_op.globals))
        return output_ops



class timecrement(snt.Module):
    # Custom sonnet module for incrementing the global feature. Yeesh
    def __init__(self,ntg,disable=False,name=None):
        self.adder = tf.constant([0.,1.],dtype=np.double)
        self.add_day = tf.Variable([[1.,0.]],dtype=np.double,trainable=False)
        self.add_tg = tf.Variable([[0.,1.]],dtype=np.double,trainable=False)
        self.T = tf.Variable([[0.,0.]],dtype=np.double,trainable=False)
        self.ntg = ntg
        self.disable = disable
    def __call__(self,T):
        if self.disable:
            return T[:,:2]
        day = T[0,0]
        tg = T[0,1]
        self.T = tf.mod(tf.add(T,self.add_tg),tf.constant([[8.,self.ntg]],dtype=np.double))
        def f1(): return tf.mod(tf.add(self.T,self.add_day),\
                                tf.constant([[7.,(self.ntg+2)]],dtype=np.double))
        def f2(): return self.T
        self.T = tf.cond(tf.math.equal(self.T[0,1],0.),f1,f2)
        return self.T


def get_node_coord_dict(h5):
    node_np = h5['node_coords']
    d = {}
    for i,coords in enumerate(node_np):
        d.update({i:(coords[0],coords[1])})
    return d

def draw_graph(graph, node_pos_dict, col_lims=None, is_normed=False, normfile=None):
    if col_lims:
        vmin,vmax = col_lims[0], col_lims[1]
        e_vmin,e_vmax = col_lims[2], col_lims[3]
    else:
        vmin,vmax = -0.5, 10
        e_vmin,e_vmax = -0.5, 5

    if is_normed:
        # Need to unnorm for plotting
        hf = h5py.File(normfile,'r')
        edgestats = hf['edge_stats']
        nodestats = hf['node_stats']
        graph = unnorm_graph(graph,nodestats,edgestats)
        hf.close()


    graphs_nx = utils_np.graphs_tuple_to_networkxs(graph)

    nodecols = graph.nodes[:,0]
    edges = graph.edges
    edgecols = np.zeros((len(edges),))
    for i,e in enumerate(graphs_nx[0].edges):
        j = np.argwhere((graph.senders==e[0]) & (graph.receivers==e[1]))
        edgecols[i] = edges[j,0]

    fig,ax = plt.subplots(figsize=(15,15))
    nx.draw(graphs_nx[0],ax=ax,pos=node_pos_dict,node_color=nodecols,
            edge_color=edgecols,node_size=100,
            cmap=plt.cm.winter,edge_cmap=plt.cm.winter,
            vmin=vmin,vmax=vmax,edge_vmin=e_vmin,edge_vmax=e_vmax,
            arrowsize=10)
    return fig,ax

def snap2graph(h5file,day,tg,use_tf=False,placeholder=False,name=None,normalize=True):
    snapstr = 'day'+str(day)+'tg'+str(tg)
    if normalize:
        edges = h5file['nn_edge_features/'+snapstr]
        nodes = h5file['nn_node_features/'+snapstr]
        glbls = h5file['nn_glbl_features/'+snapstr]
    else:
        edges = h5file['nn_edge_features/'+snapstr]
        nodes = h5file['node_features/'+snapstr]
        glbls = h5file['glbl_features/'+snapstr]
    senders = h5file['senders']
    receivers = h5file['receivers']
    
    node_arr = nodes[:]
    edge_arr = edges[:]
    glbl_arr = glbls[0]

    graphdat_dict = {
        "globals": glbl_arr.astype(np.float),
        "nodes": node_arr.astype(np.float),
        "edges": edge_arr.astype(np.float),
        "senders": senders[:],
        "receivers": receivers[:],
        "n_node": node_arr.shape[0],
        "n_edge": edge_arr.shape[0]
    }

    if not use_tf:
        graphs_tuple = utils_np.data_dicts_to_graphs_tuple([graphdat_dict])
    else:
        if placeholder:
            name = "placeholders_from_data_dicts" if not name else name
            graphs_tuple = utils_tf.placeholders_from_data_dicts([graphdat_dict], name=name)
        else:
            name = "tuple_from_data_dicts" if not name else name
            graphs_tuple = utils_tf.data_dicts_to_graphs_tuple([graphdat_dict], name=name)
            
    return graphs_tuple

def EdgeNodeCovariance(h5_name):
    h5f = h5py.File(h5_name,'a')
    try:
        covs = h5f['edge_node_covs']
        del covs, h5f['edge_node_covs']
    except:
        pass
    senders = h5f['senders']
    receivers = h5f['receivers']
    nedge = senders.shape[0]
    h5_cov = h5f.create_dataset("edge_node_covs",compression="gzip",
                                compression_opts=6,shape=(nedge,3),dtype=np.double)
    
    # Iterate over senders and edges
    # Note that these arrays have corresponding indices
    # Each edge-node pair will have 7*NTG data points, gather these.
    # We will have an array of shape=(nedge,2,3,2,7*NTG)
    # First 2 is for send/receive nodes, and second 2 is for x,y data
    np_dat = np.zeros(shape=(nedge,7*NTG,2,3),dtype=np.float)

    t = 0
    for day in range(7):
        for tg in progressbar(range(0,NTG)):
            tg_post = (tg+1)%NTG
            day_post = day
            if tg == (NTG-1):
                day_post = (day+1)%7
            edges = h5f['edge_features/day'+str(day)+'tg'+str(tg)]
            send_idxs = np.argwhere(edges[:,0] > 0).flatten()
            nodes_post = h5f['node_features/day'+str(day_post)+'tg'+str(tg_post)]

            for i in send_idxs:
                s,r = senders[i], receivers[i]
                edge = edges[i]
                x = edge[:3]
                y = nodes_post[r]

                np_dat[i,t] = np.array([x,y])
            t += 1

    for i in range(nedge):
        covs = []
        # np_dat is mostly zeros that we want to ignore
        dat = np_dat[i, np_dat[i,:,0,0]>0]
        if dat.shape[0] < 2: continue
        for j in range(3):
            covs.append(np.cov(dat[:,:,j],rowvar=False)[0,1])
        h5_cov[i] = covs

    h5f.close()

def CalcMFactor(h5_name):
    h5f = h5py.File(h5_name,'a')
    senders = h5f['senders'][:]
    receivers = h5f['receivers'][:]
    n_node = h5f.attrs['n_nodes']
    M_np = np.zeros((n_node),dtype=np.float)
    ks = np.ones((n_node),dtype=np.float)

    # Create lookup table of senders for each node
    send_edges = {}
    for i in range(n_node):
        send_edges.update({i: np.argwhere(receivers==i).flatten()})

    for day in range(7):
        for tg in progressbar(range(NTG)):
            tg_post = (tg+1)%NTG
            day_post = day
            if tg == (NTG-1):
                day_post = (day+1)%7
            nodes_post = h5f['node_features/day'+str(day_post)+'tg'+str(tg_post)]
            edges = h5f['edge_features/day'+str(day)+'tg'+str(tg)]

            ncars_n = nodes_post[:,0]
            ncars_e = edges[:,0]
            for i in range(n_node):
                ncar_e = 0
                for i_send in send_edges[i]:
                    #j = list(set(np.where(senders==sender)[0]) & set(np.where(receivers==i)[0]))
                    #assert len(j)==1
                    #ncar_e += int(j_edge[0])
                    ncar_e += ncars_e[i_send]

                if (ncar_e==0) and (ncars_n[i]==0):
                    # Nothing happening, skip this data
                    continue
                diff = ncars_n[0] - ncar_e
                M_np[i] = M_np[i] + (diff - M_np[i])/ks[i]
                ks[i] += 1

    try:
        h5f.create_dataset('M',data=M_np,compression="gzip",compression_opts=6)
    except:
        del h5f['M']
        h5f.create_dataset('M',data=M_np,compression="gzip",compression_opts=6)

    return


    
def create_nn_inputset(h5_name):
    h5f = h5py.File(h5_name,'a')

    try:
        covs = h5f['edge_node_covs']
    except:
        print("edge_node_covs DNE, exiting.")
        h5f.close()
        return

    try:
        M = h5f['M']
    except:
        print("M factor dataset DNE, exiting.")
        h5f.close()
        return

    try:
        nn_edgegroup = h5f.create_group("nn_edge_features")
    except:
        print("nn_edge_features group already exists. Overwriting")
        del h5f['nn_edge_features']
        nn_edgegroup = h5f.create_group("nn_edge_features")
    try:
        nn_nodegroup = h5f.create_group("nn_node_features")
    except:
        print("nn_node_features group already exists. Overwriting")
        del h5f['nn_node_features']
        nn_nodegroup = h5f.create_group("nn_node_features")
    try:
        nn_glblgroup = h5f.create_group("nn_glbl_features")
    except:
        print("nn_glbl_features group already exists. Overwriting")
        del h5f['nn_glbl_features']
        nn_glblgroup = h5f.create_group("nn_glbl_features")

    node_stats = np.zeros((2,4),dtype=np.float64)
    edge_stats = np.zeros((2,13),dtype=np.float64)
    glbl_stats = np.zeros((2,2),dtype=np.float64)
    nk, ek = 1, 1

    n_edge = h5f.attrs['n_edges']
    n_node = h5f.attrs['n_nodes']
    for d in progressbar(range(7)):
        for tg in range(NTG):
            snapstr = "day"+str(d)+"tg"+str(tg)
            e_fts = np.zeros((n_edge,13),dtype=np.float64)
            edges = h5f['edge_features/'+snapstr]
            e_fts[:,:4] = edges[:]
            e_fts[:,4:7] = covs[:]
            e_fts[:,7:10] = covs[:]*edges[:,:3]
            e_fts[:,10] = edges[:,0]*edges[:,1]
            e_fts[:,11] = (DTG/60.)*edges[:,1]/edges[:,3]
            e_fts[:,12] = e_fts[:,11] * edges[:,0]

            n_fts = np.zeros((n_node,4),dtype=np.float64)
            nodes = h5f['node_features/'+snapstr]
            n_fts[:,:3] = nodes[:]
            n_fts[:,3] = M[:]

            # You could do some stat stuff here
            np.random.shuffle(e_fts)
            np.random.shuffle(n_fts)
            for e_ft in e_fts[:50]:
                m_k = edge_stats[0,:] + (e_ft - edge_stats[0,:])/ek
                edge_stats[1,:] = edge_stats[1,:] + (e_ft - edge_stats[0,:])*(e_ft - m_k)
                edge_stats[0,:] = m_k
                ek+=1
            for n_ft in n_fts[:20]:
                m_k = node_stats[0,:] + (n_ft - node_stats[0,:])/nk
                node_stats[1,:] = node_stats[1,:] + (n_ft - node_stats[0,:])*(n_ft - m_k)
                node_stats[0,:] = m_k
                nk+=1

            nn_edgegroup.create_dataset(snapstr,data=e_fts,compression="gzip",compression_opts=6)
            nn_nodegroup.create_dataset(snapstr,data=n_fts,compression='gzip',compression_opts=6)

    print("Creating normalized dataset")

    #
    #
    # Lets not have separate datasets for the unnormed and normed features
    # Just overwrite the unnormed stuff with the normed stuff
    #
    #


    try:
        normed_edge_group = h5f.create_group("nn_edge_features_normed")
        normed_node_group = h5f.create_group("nn_node_features_normed")
        normed_glbl_group = h5f.create_group("nn_glbl_features_normed")
    except:
        print("Normed features exist. Overwriting")
        del h5f['nn_edge_features_normed'], h5f['nn_node_features_normed'],\
            h5f['nn_glbl_features_normed']
        normed_edge_group = h5f.create_group("nn_edge_features_normed")
        normed_node_group = h5f.create_group("nn_node_features_normed")
        normed_glbl_group = h5f.create_group("nn_glbl_features_normed")

    """
    print("Calculating norm stats")
    node_stats = np.zeros((2,4),dtype=np.float64)
    edge_stats = np.zeros((2,13),dtype=np.float64)
    glbl_stats = np.zeros((2,2),dtype=np.float64)
    glblgroup = h5f['glbl_features']

    # This loop goes over each tg and then each
    # edge or node within the tg
    # This is costly, let's do Sun,Mon,Thur,Sat
    # and some random selection of 25% of the nodes/edges
    # ...later
    #
    # You could also in theory gather stats as you build the
    # nn_nodegroup.items in the above loops
    #
    k = 1 # data iter
    for key,dset in progressbar(nn_nodegroup.items()):
        np.random.shuffle(dset)
        for row in dset[:10]:
            m_k = node_stats[0,:] + (row - node_stats[0,:])/k
            node_stats[1,:] = node_stats[1,:] + (row - node_stats[0,:])*(row - m_k)
            node_stats[0,:] = m_k
            k+=1
    node_stats[1,:] = np.sqrt(node_stats[1,:]/(k-1))
    
    k = 1
    for key,dset in progressbar(nn_edgegroup.items()):
        np.random.shuffle(dset)
        for row in dset[:10]:
            m_k = edge_stats[0,:] + (row - edge_stats[0,:])/k
            edge_stats[1,:] = edge_stats[1,:] + (row - edge_stats[0,:])*(row - m_k)
            edge_stats[0,:] = m_k
            k+=1
    edge_stats[1,:] = np.sqrt(edge_stats[1,:]/(k-1))
    """

    node_stats[1,:] = np.sqrt(node_stats[1,:]/(nk-1))
    edge_stats[1,:] = np.sqrt(edge_stats[1,:]/(ek-1))
    glbl_stats[:] = [[3.0,2.0], [np.mean(range(NTG)),np.std(range(NTG))]]

    glblgroup = h5f['glbl_features']
    # Now that we have the norm stats, we apply it to the existing datasets
    print("Applying norm to feature sets")
    for d in progressbar(range(7)):
        for tg in range(NTG):
            snapstr="day"+str(d)+"tg"+str(tg)
            nodes = nn_nodegroup[snapstr]
            edges = nn_edgegroup[snapstr]
            glbls = glblgroup[snapstr]
            normed_nodes = mynorm(nodes,node_stats[0,:],node_stats[1,:])
            normed_edges = mynorm(edges,edge_stats[0,:],edge_stats[1,:])
            normed_glbls = mynorm(glbls,glbl_stats[0,:],glbl_stats[1,:])

            if 0:
                normed_node_group.create_dataset(snapstr,compression="gzip",compression_opts=6,
                                                 data=normed_nodes)
                normed_edge_group.create_dataset(snapstr,compression="gzip",compression_opts=6,
                                                 data=normed_edges)
                normed_glbl_group.create_dataset(snapstr,compression="gzip",compression_opts=6,
                                                 data=normed_glbls)
            else:
                nodes[:] = normed_nodes.copy()
                edges[:] = normed_edges.copy()
                nn_glblgroup.create_dataset(snapstr,data=normed_glbls,compression='gzip',compression_opts=6)

    # Save the stats to hdf5
    try:
        h5f.create_dataset('node_stats',compression="gzip",compression_opts=6,data=node_stats)
        h5f.create_dataset('edge_stats',compression="gzip",compression_opts=6,data=edge_stats)
        h5f.create_dataset('glbl_stats',compression="gzip",compression_opts=6,data=glbl_stats)
    except:
        del h5f['node_stats'], h5f['edge_stats'], h5f['glbl_stats']
        h5f.create_dataset('node_stats',compression="gzip",compression_opts=6,data=node_stats)
        h5f.create_dataset('edge_stats',compression="gzip",compression_opts=6,data=edge_stats)
        h5f.create_dataset('glbl_stats',compression="gzip",compression_opts=6,data=glbl_stats)

    h5f.close()


def mynorm(nparr,mus,stds):
    return np.divide(np.subtract(nparr,mus),stds)

def my_unnorm(nparr,norms):
    return np.add(np.multiply(nparr,norms[1,:]),norms[0,:])

def unnorm_graph(graph, node_norms, edge_norms):
    return graph.replace(nodes=my_unnorm(graph.nodes,node_norms),
                         edges=my_unnorm(graph.edges,edge_norms))
                                         
    
def copy_graph(graphs_tuple):
    return utils_np.data_dicts_to_graphs_tuple(
        utils_np.graphs_tuple_to_data_dicts(graphs_tuple))


def get_daytimes():
    daytimes = np.zeros((7*NTG,2),dtype=int)
    i=0
    for d in range(7):
        for tg in range(NTG):
            daytimes[i] = [d,tg]
            i+=1
    return daytimes
    

def get_norm_stats(hfname):
    h5f = h5py.File(hfname,'a')
    node_stats = np.zeros((2,5),dtype=np.float64)
    edge_stats = np.zeros((2,13),dtype=np.float64)
    glbl_stats = np.zeros((2,2),dtype=np.float64)
    nodegroup = h5f['nn_node_features']
    edgegroup = h5f['nn_edge_features']
    glblgroup = h5f['glbl_features']

    print("Calculating norm stats")
    k = 1 # data iter
    for key,dset in progressbar(nodegroup.items()):
        for row in dset:
            m_k = node_stats[0,:] + (row - node_stats[0,:])/k
            node_stats[1,:] = node_stats[1,:] + (row - node_stats[0,:])*(row - m_k)
            node_stats[0,:] = m_k
            k+=1
    node_stats[1,:] = np.sqrt(node_stats[1,:]/(k-1))
    
    k = 1
    for key,dset in progressbar(edgegroup.items()):
        for row in dset:
            m_k = edge_stats[0,:] + (row - edge_stats[0,:])/k
            edge_stats[1,:] = edge_stats[1,:] + (row - edge_stats[0,:])*(row - m_k)
            edge_stats[0,:] = m_k
            k+=1
    edge_stats[1,:] = np.sqrt(edge_stats[1,:]/(k-1))

    glbl_stats[:] = [[3.0,2.0], [np.mean(range(NTG)),np.std(range(NTG))]]

    # Save the stats to hdf5
    try:
        del h5f['node_stats'],h5f['edge_stats'],h5f['glbl_stats']
    except:
        pass
    h5f.create_dataset('node_stats',compression="gzip",compression_opts=6,data=node_stats)
    h5f.create_dataset('edge_stats',compression="gzip",compression_opts=6,data=edge_stats)
    h5f.create_dataset('glbl_stats',compression="gzip",compression_opts=6,data=glbl_stats)

    h5f.close()

    return






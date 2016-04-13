import React from 'react';  
import {transitionTo, Router, browserHistory, 
  DefaultRoute, Link, Route, RouteHandler } from 'react-router';
import ReactDOM from 'react-dom'
var request = require('superagent');

import Header from './components/header.js'
import Footer from './components/footer.js'

import Wait from './components/wait.js'
import Res from "./components/results.js"

// var settings = {
//   doBuildTree:true,
//   shouldReuseBranchLen:false,
//   doReroot:false,
//   gtr:"Jukes-Cantor",
//   shouldUseBranchLenPenalty:{
//       bool:true,
//       value:0.0
//   },
//   shouldUseSlope:{
//       bool:true,
//       value:0.0
//   },
//   doResolvePoly: false,
//   doCoalescent:{
//     bool:true,
//     Tc:0.0
//   },
//   doRelaxedClock:{
//     bool:true,
//     alpha:0.0,
//     beta:0.0
//   },
//   doRootJoint:true,
//   doCalcGTR:true
// };

var Main  = React.createClass( {
  
  getInitialState() {
      return {
        UID: "sdf", 
        settings:settings,
        state: {},      
        tree_file:false,
        aln_file:false,
        meta_file:false
      };
  },
  
  componentDidMount() {
    this.req_uid();
    //browserHistory.push('/zvsjdlgz/app'); 
  },
  
  req_uid() {
      request
      .post('/')
      .end(this.on_uid_received);
    
  },
  
  on_uid_received(req, res, err){
    var uid = JSON.parse(res.text).redirect;
    this.setState({UID:uid});
    console.log(this.state.UID);
    browserHistory.push(this.state.UID + '/app/'); 
  },
  
  handle_run(){
    console.log("APP:: RUN button pressed");
    if ((!this.state.tree_file & !this.state.settings.doBuildTree) || ! this.state.aln_file || !this.state.meta_file){
      var msg = "Cannot proceed with TreeTime: one or more file not loaded.\n\n"
      if ((!this.state.tree_file & !this.state.settings.doBuildTree)){
        msg += "Phylogenetic tree file is missing.\n\n";
      }
      if (!this.state.aln_file){
        msg += "Sequence alignment file is missing.\n\n";
      }
      if (!this.state.meta_file){
        msg += "Meta data file is missing.\n\n";
      }
      alert(msg);
      return;
    }
    request.post("/" + this.state.UID + "/run/")
      .set('Content-Type', 'application/json')
      .send({settings: this.state.settings})
      .end(this.on_run_status);
  },

  on_run_status(err, res){
    var status = JSON.parse(res.text).status;
    console.log(status);
    if (status == "OK"){
      browserHistory.push('wait/'); 
    }
  },

  on_settings_changed(name, setting){
      console.log("APP:: settings changed. " + name + " new value = " + setting);
      var settings = this.state.settings
      settings[name] = setting;

      this.setState({settings: settings})
      this.state.settings = settings;
  },

  on_state_changed(name, state){

  },

  on_all_done(){
      console.log("ALL don, redirecting to RESULTS page");
      browserHistory.push('results/'); 
  },

  setAppState : function(partialState){
    this.setState(partialState);
  },

  render(){
    return (
        <div>
          <Header/>
           {this.props.children && React.cloneElement(
               this.props.children, 
               {
                 UID:this.state.UID,
                 appState:this.state,
                 setAppState:this.setAppState,
                 settings:this.state.settings,
                 state:this.state.state,
                 handle_run: this.handle_run,
                 handle_settings_change: this.on_settings_changed,
                 handle_state_changed: this.on_state_changed, 
                 handle_all_done: this.on_all_done
               }
             )}
          <Footer/>
        </div>
    );
  }
});

ReactDOM.render((
   //<Router history={browserHistory} >
   //  <Route path="/" component={TreeTimeForm}>
   //    <Route path="/:user_id/app/" component={TreeTimeForm} />
   //    <Route path="wait/" component={Wait} />
   //    <Route path="results/" component={Res} />
   //  </Route>
   //</Router>),
<Main settings={settings}/>),
document.getElementById('react'));

        
